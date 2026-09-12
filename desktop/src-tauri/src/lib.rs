use serde_json::{json, Map, Value};
use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use tauri::{AppHandle, Emitter, Manager};

/// RPC methods the window is allowed to reach. The worker speaks more than
/// this over stdin, but only these are exposed to page code.
const WORKER_METHODS: &[&str] = &[
    "describe",
    "ping",
    "create_workspace",
    "open_workspace",
    "save_document",
    "plan_grid",
    "set_grid",
    "save_sketch",
    "preview_mask",
    "run_flow",
    "run_cli",
    "check_update",
    "open_url",
    "get_surfaces",
    "get_section",
    "get_top_view",
    "export_mesh",
    "save_image",
    "import_gds",
    "export_recipes_xlsx",
    "import_recipes_xlsx",
    "gds_layers",
    "list_sketches",
];

/// Methods that may name a directory that does not exist yet.
const CREATES_WORKSPACE: &[&str] = &["create_workspace"];

const WORKER_EVENT: &str = "process-studio-worker";

fn canonical_directory(path: &str) -> Result<PathBuf, String> {
    let resolved = PathBuf::from(path)
        .canonicalize()
        .map_err(|error| format!("Cannot access {path}: {error}"))?;
    if !resolved.is_dir() {
        return Err(format!("{path} is not a directory."));
    }
    Ok(resolved)
}

/// Resolve the workspace root, allowing a not-yet-created directory only for
/// the method that is supposed to create one.
fn resolve_root(method: &str, root: &str) -> Result<String, String> {
    if CREATES_WORKSPACE.contains(&method) {
        let candidate = PathBuf::from(root);
        if candidate.is_dir() {
            return Ok(canonical_directory(root)?.to_string_lossy().into_owned());
        }
        let parent = candidate
            .parent()
            .ok_or_else(|| "The new workspace has no parent directory.".to_string())?;
        let parent = canonical_directory(&parent.to_string_lossy())?;
        let name = candidate
            .file_name()
            .ok_or_else(|| "The new workspace has no directory name.".to_string())?;
        return Ok(parent.join(name).to_string_lossy().into_owned());
    }
    Ok(canonical_directory(root)?.to_string_lossy().into_owned())
}

fn development_worker_command() -> Command {
    let python = std::env::var("PROCESS_STUDIO_PYTHON").unwrap_or_else(|_| "python3".to_string());
    let repository_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let source_directory = repository_root.join("src");
    let mut python_paths = vec![source_directory];
    if let Some(existing) = std::env::var_os("PYTHONPATH") {
        python_paths.extend(std::env::split_paths(&existing));
    }
    let python_path = std::env::join_paths(python_paths).unwrap_or_default();
    let mut command = Command::new(python);
    command
        .arg("-m")
        .arg("process_studio.worker")
        .current_dir(&repository_root)
        .env("PYTHONPATH", python_path);
    command
}

const WORKER_RELATIVE_PATH: &str = if cfg!(target_os = "windows") {
    "resources/worker/process-studio-worker.exe"
} else {
    "resources/worker/process-studio-worker"
};

/// Where a packaged worker can live, in the order they are tried.
///
/// The platform resource directory is the installed location: `Contents/
/// Resources` in a .app, `/usr/lib/<product>` for a Linux package. An
/// unpacked build instead keeps the worker beside the executable, which is
/// also where the Windows build looks first, so both layouts run.
fn packaged_worker_candidates(app: &AppHandle) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(resources) = app.path().resource_dir() {
        candidates.push(resources.join(WORKER_RELATIVE_PATH));
    }
    if let Some(directory) = std::env::current_exe().ok().and_then(|path| {
        path.parent().map(Path::to_path_buf)
    }) {
        candidates.push(directory.join(WORKER_RELATIVE_PATH));
    }
    candidates
}

fn packaged_worker_command(app: &AppHandle) -> Result<Command, String> {
    if let Ok(explicit) = std::env::var("PROCESS_STUDIO_WORKER") {
        return Ok(Command::new(explicit));
    }
    let candidates = packaged_worker_candidates(app);
    if let Some(executable) = candidates.iter().find(|path| path.is_file()) {
        return Ok(Command::new(executable));
    }
    Err(format!(
        "The packaged process worker was not found. Looked in: {}",
        candidates
            .iter()
            .map(|path| path.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    ))
}

/// Windows shows a console window for every console process a GUI app starts.
/// The worker is a console program because it speaks JSON over stdio, and the
/// shell starts one per request, so without this the window flashes up on each
/// call and stays for the length of a run.
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn worker_command(app: &AppHandle) -> Result<Command, String> {
    #[allow(unused_mut)] // only the Windows branch below needs it
    let mut command = if cfg!(debug_assertions) {
        development_worker_command()
    } else {
        packaged_worker_command(app)?
    };
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    Ok(command)
}

/// One answer to one request, as the worker reported it.
type Reply = Result<Value, String>;

/// Lines of stderr kept for the crash message. The worker is quiet on
/// stderr in normal operation, so what is here is what went wrong.
const STDERR_TAIL_LINES: usize = 40;

/// A worker process that lives for the whole session.
///
/// Requests are written to its stdin as they come and matched to answers by
/// id, so several can be in flight; the worker itself decides the order it
/// runs them in. When the process ends, every request still waiting is
/// answered with the failure, and the next request starts a fresh process.
struct Worker {
    child: Mutex<Child>,
    stdin: Mutex<ChildStdin>,
    pending: Mutex<HashMap<String, mpsc::Sender<Reply>>>,
    alive: AtomicBool,
    stderr_tail: Mutex<VecDeque<String>>,
    /// Where progress and log lines go: the window, in the application.
    events: EventSink,
    /// Runs once when the process ends, so its owner can let go of it.
    on_exit: Mutex<Option<Box<dyn FnOnce() + Send>>>,
}

/// Receives every event the worker emits, in the order it emitted them.
type EventSink = Arc<dyn Fn(Value) + Send + Sync>;

impl Worker {
    fn spawn(
        mut command: Command,
        events: EventSink,
        on_exit: impl FnOnce() + Send + 'static,
    ) -> Result<Arc<Worker>, String> {
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|error| format!("Cannot start the process worker: {error}"))?;
        let stdin = child.stdin.take().ok_or("Worker stdin is unavailable.")?;
        let stdout = child.stdout.take().ok_or("Worker stdout is unavailable.")?;
        let stderr = child.stderr.take().ok_or("Worker stderr is unavailable.")?;
        let worker = Arc::new(Worker {
            child: Mutex::new(child),
            stdin: Mutex::new(stdin),
            pending: Mutex::new(HashMap::new()),
            alive: AtomicBool::new(true),
            stderr_tail: Mutex::new(VecDeque::new()),
            events,
            on_exit: Mutex::new(Some(Box::new(on_exit))),
        });

        let for_stderr = Arc::clone(&worker);
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                let mut tail = for_stderr.stderr_tail.lock().unwrap();
                if tail.len() == STDERR_TAIL_LINES {
                    tail.pop_front();
                }
                tail.push_back(line);
            }
        });

        let for_stdout = Arc::clone(&worker);
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                for_stdout.deliver(&line);
            }
            for_stdout.finish();
        });
        Ok(worker)
    }

    fn emit(&self, event: Value) {
        (self.events)(event);
    }

    /// Route one output line: progress goes to the window, answers to the
    /// request that asked. A line that is neither is a worker bug and is
    /// reported to the window as a log line rather than dropped.
    fn deliver(&self, line: &str) {
        let message: Value = match serde_json::from_str(line) {
            Ok(value) => value,
            Err(error) => {
                self.emit(json!({
                    "kind": "log",
                    "message": format!("The worker emitted invalid JSON: {error}"),
                }));
                return;
            }
        };
        let id = message.get("id").map(id_string);
        match message.get("kind").and_then(Value::as_str) {
            Some("event") => {
                if let Some(Value::Object(mut event)) = message.get("event").cloned() {
                    if let Some(id) = id {
                        event.insert("requestId".to_string(), Value::String(id));
                    }
                    self.emit(Value::Object(event));
                }
            }
            Some("response") => {
                let Some(id) = id else { return };
                let reply = if message.get("ok").and_then(Value::as_bool) == Some(true) {
                    message
                        .get("result")
                        .cloned()
                        .ok_or_else(|| "The worker response has no result.".to_string())
                } else {
                    Err(message
                        .pointer("/error/message")
                        .and_then(Value::as_str)
                        .unwrap_or("Unknown worker error")
                        .to_string())
                };
                if let Some(sender) = self.pending.lock().unwrap().remove(&id) {
                    let _ = sender.send(reply);
                }
            }
            _ => self.emit(json!({
                "kind": "log",
                "message": "The worker emitted an unknown message kind.",
            })),
        }
    }

    /// The process is gone: fail whatever was waiting and step aside so the
    /// next request starts a new one.
    fn finish(&self) {
        self.alive.store(false, Ordering::SeqCst);
        let status = self
            .child
            .lock()
            .unwrap()
            .wait()
            .map(|status| status.to_string())
            .unwrap_or_else(|error| format!("unknown status ({error})"));
        let tail: Vec<String> = self.stderr_tail.lock().unwrap().iter().cloned().collect();
        let reason = if tail.is_empty() {
            format!("The process worker exited ({status}).")
        } else {
            format!("The process worker exited ({status}): {}", tail.join("\n"))
        };
        let waiting: Vec<mpsc::Sender<Reply>> = self.pending.lock().unwrap().drain().map(|(_, s)| s).collect();
        for sender in waiting {
            let _ = sender.send(Err(reason.clone()));
        }
        if let Some(on_exit) = self.on_exit.lock().unwrap().take() {
            on_exit();
        }
        self.emit(json!({ "kind": "log", "message": reason }));
    }

    fn write_line(&self, line: &str) -> Result<(), String> {
        let mut stdin = self.stdin.lock().unwrap();
        stdin
            .write_all(line.as_bytes())
            .and_then(|_| stdin.write_all(b"\n"))
            .and_then(|_| stdin.flush())
            .map_err(|error| format!("Cannot send the request to the worker: {error}"))
    }

    fn request(&self, id: String, method: String, params: Value) -> Reply {
        let (sender, receiver) = mpsc::channel();
        self.pending.lock().unwrap().insert(id.clone(), sender);
        let request = json!({ "kind": "request", "id": id, "method": method, "params": params });
        let line = serde_json::to_string(&request)
            .map_err(|error| format!("Cannot encode the worker request: {error}"))?;
        if let Err(error) = self.write_line(&line) {
            self.pending.lock().unwrap().remove(&id);
            return Err(error);
        }
        receiver
            .recv()
            .unwrap_or_else(|_| Err("The process worker went away before answering.".to_string()))
    }

    fn cancel(&self, id: &str) -> Result<(), String> {
        let line = serde_json::to_string(&json!({ "kind": "cancel", "id": id }))
            .map_err(|error| format!("Cannot encode the cancel message: {error}"))?;
        self.write_line(&line)
    }
}

fn id_string(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// The session's worker, started on first use and replaced when it exits.
#[derive(Default)]
struct WorkerHost {
    current: Mutex<Option<Arc<Worker>>>,
    next_id: AtomicU64,
}

impl WorkerHost {
    fn worker(&self, app: &AppHandle) -> Result<Arc<Worker>, String> {
        let mut current = self.current.lock().unwrap();
        if let Some(worker) = current.as_ref() {
            if worker.alive.load(Ordering::SeqCst) {
                return Ok(Arc::clone(worker));
            }
        }
        let for_events = app.clone();
        let for_exit = app.clone();
        let worker = Worker::spawn(
            worker_command(app)?,
            Arc::new(move |event| {
                let _ = for_events.emit(WORKER_EVENT, event);
            }),
            move || {
                if let Some(host) = for_exit.try_state::<WorkerHost>() {
                    // Only the process that ended steps aside; a replacement
                    // started meanwhile is left in place.
                    let mut current = host.current.lock().unwrap();
                    if current.as_ref().is_some_and(|w| !w.alive.load(Ordering::SeqCst)) {
                        *current = None;
                    }
                }
            },
        )?;
        *current = Some(Arc::clone(&worker));
        Ok(worker)
    }

    fn fresh_id(&self) -> String {
        format!("r{}", self.next_id.fetch_add(1, Ordering::SeqCst) + 1)
    }
}

#[tauri::command]
async fn worker_invoke(
    app: AppHandle,
    method: String,
    params: Value,
    request_id: Option<String>,
) -> Result<Value, String> {
    if !WORKER_METHODS.contains(&method.as_str()) {
        return Err(format!("{method} is not an allowed worker method."));
    }
    let mut params = match params {
        Value::Null => Map::new(),
        Value::Object(map) => map,
        _ => return Err("Worker params must be an object.".to_string()),
    };
    if let Some(root) = params.get("root").and_then(Value::as_str) {
        let resolved = resolve_root(&method, root)?;
        params.insert("root".to_string(), Value::String(resolved));
    }
    let host = app.state::<WorkerHost>();
    let id = match request_id.filter(|id| !id.trim().is_empty()) {
        Some(id) => id,
        None => host.fresh_id(),
    };
    let worker = host.worker(&app)?;
    tauri::async_runtime::spawn_blocking(move || worker.request(id, method, Value::Object(params)))
        .await
        .map_err(|error| format!("The worker task failed: {error}"))?
}

/// Withdraw a request. A queued one is answered as cancelled at once; a
/// running one stops at its next boundary and answers then.
#[tauri::command]
fn worker_cancel(app: AppHandle, request_id: String) -> Result<(), String> {
    let host = app.state::<WorkerHost>();
    let current = host.current.lock().unwrap();
    match current.as_ref() {
        Some(worker) if worker.alive.load(Ordering::SeqCst) => worker.cancel(&request_id),
        _ => Ok(()),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(WorkerHost::default())
        .invoke_handler(tauri::generate_handler![worker_invoke, worker_cancel])
        .run(tauri::generate_context!())
        .expect("error while running Process Studio");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use std::time::Duration;

    fn python_worker() -> Command {
        let repository_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let mut command = Command::new(
            std::env::var("PROCESS_STUDIO_PYTHON").unwrap_or_else(|_| "python3".to_string()),
        );
        command
            .arg("-m")
            .arg("process_studio.worker")
            .current_dir(&repository_root)
            .env("PYTHONPATH", repository_root.join("src"));
        command
    }

    fn spawn_test_worker(
        events: Arc<Mutex<Vec<Value>>>,
        exits: Arc<AtomicUsize>,
    ) -> Arc<Worker> {
        let sink_events = Arc::clone(&events);
        Worker::spawn(
            python_worker(),
            Arc::new(move |event| sink_events.lock().unwrap().push(event)),
            move || {
                exits.fetch_add(1, Ordering::SeqCst);
            },
        )
        .expect("the python worker starts")
    }

    #[test]
    fn one_process_answers_many_requests_by_id() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let exits = Arc::new(AtomicUsize::new(0));
        let worker = spawn_test_worker(events, Arc::clone(&exits));
        let first = worker.request("a".into(), "ping".into(), json!({})).unwrap();
        assert!(first.get("protocolVersion").is_some());
        // The same process, still alive, answers a second time.
        let second = worker.request("b".into(), "describe".into(), json!({})).unwrap();
        assert!(second.get("kernels").is_some());
        assert!(worker.alive.load(Ordering::SeqCst));
        assert_eq!(exits.load(Ordering::SeqCst), 0);
        let refused = worker.request("c".into(), "teleport".into(), json!({}));
        assert!(refused.unwrap_err().contains("Unknown RPC method"));
    }

    #[test]
    fn concurrent_requests_each_get_their_own_answer() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let worker = spawn_test_worker(events, Arc::new(AtomicUsize::new(0)));
        let handles: Vec<_> = (0..6)
            .map(|index| {
                let worker = Arc::clone(&worker);
                thread::spawn(move || {
                    let method = if index % 2 == 0 { "ping" } else { "describe" };
                    (
                        index,
                        worker.request(format!("r{index}"), method.into(), json!({})).unwrap(),
                    )
                })
            })
            .collect();
        for handle in handles {
            let (index, answer) = handle.join().unwrap();
            if index % 2 == 0 {
                assert!(answer.get("protocolVersion").is_some(), "ping answer for {index}");
            } else {
                assert!(answer.get("kernels").is_some(), "describe answer for {index}");
            }
        }
    }

    #[test]
    fn a_run_reports_progress_with_its_request_id_and_can_be_stopped() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let worker = spawn_test_worker(Arc::clone(&events), Arc::new(AtomicUsize::new(0)));
        let root = std::env::temp_dir().join(format!("ps-worker-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        worker
            .request(
                "create".into(),
                "create_workspace".into(),
                json!({ "root": root.to_string_lossy(), "name": "Stop me" }),
            )
            .unwrap();
        let runner = Arc::clone(&worker);
        let root_text = root.to_string_lossy().into_owned();
        let run = thread::spawn(move || {
            runner.request("run".into(), "run_flow".into(), json!({ "root": root_text }))
        });
        // Wait for the first step to start, then withdraw the run.
        let deadline = std::time::Instant::now() + Duration::from_secs(60);
        loop {
            let started = events.lock().unwrap().iter().any(|event| {
                event.get("requestId").and_then(Value::as_str) == Some("run")
                    && event
                        .get("message")
                        .and_then(Value::as_str)
                        .is_some_and(|m| m.starts_with("Running"))
            });
            if started || std::time::Instant::now() > deadline {
                break;
            }
            thread::sleep(Duration::from_millis(20));
        }
        worker.cancel("run").unwrap();
        let outcome = run.join().unwrap();
        // On a fast machine the starter flow can finish before the cancel
        // lands; the deterministic cancel test lives in the Python suite.
        // What this test pins is the plumbing: progress carried the id, the
        // cancel line was accepted, and the process survived either way.
        match outcome {
            Ok(_) => {}
            Err(message) => assert!(message.contains("Stopped before"), "{message}"),
        }
        assert!(events.lock().unwrap().iter().any(|event| {
            event.get("requestId").and_then(Value::as_str) == Some("run")
        }));
        // The process is still the same one and still serves.
        assert!(worker.alive.load(Ordering::SeqCst));
        worker.request("after".into(), "ping".into(), json!({})).unwrap();
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_dead_process_fails_what_waited_and_reports_it() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let exits = Arc::new(AtomicUsize::new(0));
        let worker = spawn_test_worker(Arc::clone(&events), Arc::clone(&exits));
        worker.request("warm".into(), "ping".into(), json!({})).unwrap();
        let waiter = Arc::clone(&worker);
        let waiting = thread::spawn(move || {
            waiter.request("slow".into(), "describe".into(), json!({}))
        });
        // Killing the process is the crash; everything waiting learns of it.
        worker.child.lock().unwrap().kill().unwrap();
        let outcome = waiting.join().unwrap();
        match outcome {
            Ok(_) => {} // the answer beat the kill; nothing to assert
            Err(message) => assert!(message.contains("exited"), "{message}"),
        }
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        while exits.load(Ordering::SeqCst) == 0 && std::time::Instant::now() < deadline {
            thread::sleep(Duration::from_millis(20));
        }
        assert_eq!(exits.load(Ordering::SeqCst), 1);
        assert!(!worker.alive.load(Ordering::SeqCst));
        let after = worker.request("late".into(), "ping".into(), json!({}));
        assert!(after.is_err());
    }
}

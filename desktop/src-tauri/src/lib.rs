use serde_json::{json, Map, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
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
    "run_flow",
    "get_surfaces",
    "get_section",
    "get_top_view",
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

fn worker_command(app: &AppHandle) -> Result<Command, String> {
    if cfg!(debug_assertions) {
        Ok(development_worker_command())
    } else {
        packaged_worker_command(app)
    }
}

fn call_worker(app: AppHandle, method: String, params: Value) -> Result<Value, String> {
    let mut command = worker_command(&app)?;
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("Cannot start the process worker: {error}"))?;
    let request = json!({ "kind": "request", "id": 1, "method": method, "params": params });
    if let Some(mut input) = child.stdin.take() {
        let encoded = serde_json::to_vec(&request)
            .map_err(|error| format!("Cannot encode the worker request: {error}"))?;
        input
            .write_all(&encoded)
            .and_then(|_| input.write_all(b"\n"))
            .map_err(|error| format!("Cannot send the request to the worker: {error}"))?;
    }

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Worker stdout is unavailable.".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "Worker stderr is unavailable.".to_string())?;
    let stderr_reader = thread::spawn(move || {
        let mut reader = BufReader::new(stderr);
        let mut output = String::new();
        std::io::Read::read_to_string(&mut reader, &mut output)
            .map(|_| output)
            .unwrap_or_default()
    });

    let mut response: Option<Value> = None;
    for line in BufReader::new(stdout).lines() {
        let line = line.map_err(|error| format!("Cannot read the worker output: {error}"))?;
        let message: Value = serde_json::from_str(&line)
            .map_err(|error| format!("The worker emitted invalid JSON: {error}"))?;
        match message.get("kind").and_then(Value::as_str) {
            Some("event") => {
                if let Some(event) = message.get("event") {
                    let _ = app.emit(WORKER_EVENT, event.clone());
                }
            }
            Some("response") => response = Some(message),
            _ => return Err("The worker emitted an unknown message kind.".to_string()),
        }
    }

    let status = child
        .wait()
        .map_err(|error| format!("Cannot collect the worker status: {error}"))?;
    let stderr_output = stderr_reader.join().unwrap_or_default();
    if !status.success() {
        return Err(format!("The process worker exited with {status}: {stderr_output}"));
    }
    let response = response.ok_or_else(|| "The process worker returned no response.".to_string())?;
    if response.get("ok").and_then(Value::as_bool) != Some(true) {
        let message = response
            .pointer("/error/message")
            .and_then(Value::as_str)
            .unwrap_or("Unknown worker error");
        return Err(message.to_string());
    }
    response
        .get("result")
        .cloned()
        .ok_or_else(|| "The worker response has no result.".to_string())
}

#[tauri::command]
async fn worker_invoke(app: AppHandle, method: String, params: Value) -> Result<Value, String> {
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
    tauri::async_runtime::spawn_blocking(move || call_worker(app, method, Value::Object(params)))
        .await
        .map_err(|error| format!("The worker task failed: {error}"))?
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![worker_invoke])
        .run(tauri::generate_context!())
        .expect("error while running Process Studio");
}

import type { ParameterValue, ProcessType } from "../types";

/** One parameter a process type understands, and how to show it. */
export interface ParameterSpec {
  key: string;
  label: string;
  /** The unit the stored value is in; a unit picker may offer others. */
  unit?: string;
  kind?: "number" | "text" | "mode";
  /** Suggestions for a text field; it still takes anything typed. */
  options?: string[];
  initial: ParameterValue;
  hint?: string;
  /** Kernels that read this parameter; absent means every kernel does. */
  kernels?: string[];
}

/** Every field a process type has, for the experiment record: what was done. */
export const PROCESS_SPECS: Record<ProcessType, ParameterSpec[]> = {
  deposit: [
    { key: "target", label: "Target thickness", unit: "µm", initial: 0.05 },
    { key: "cycles", label: "Cycles", initial: 100, hint: "Whole cycles of the recipe." },
    {
      key: "rate_per_cycle",
      label: "Rate per cycle",
      unit: "µm/cycle",
      initial: 0.0001,
      hint: "Cycles × rate per cycle is the thickness, unless a target says otherwise.",
    },
    { key: "rate", label: "Deposition rate", unit: "µm/min", initial: 0.01 },
    { key: "time_min", label: "Time", unit: "min", initial: 1 },
    { key: "temperature_c", label: "Temperature", unit: "°C", initial: 25 },
    { key: "mode", label: "Deposition mode", kind: "mode", initial: "conformal" },
  {
    key: "resolution",
    label: "Resolution (this step)",
    unit: "µm",
    initial: 0.01,
    hint: "Grid for this step only; leave it out to use the project's. Coarser is faster.",
  },
  ],
  etch: [
    { key: "target", label: "Target depth", unit: "µm", initial: 0.1 },
    { key: "time_min", label: "Time", unit: "min", initial: 1 },
    { key: "temperature_c", label: "Temperature", unit: "°C", initial: 25 },
    {
      key: "directional_fraction",
      label: "Directional fraction",
      initial: 1,
      hint: "1 is vertical; 0 is isotropic.",
    },
  {
    key: "resolution",
    label: "Resolution (this step)",
    unit: "µm",
    initial: 0.01,
    hint: "Grid for this step only; leave it out to use the project's. Coarser is faster.",
  },
  ],
  cmp: [
    { key: "target_z", label: "Planarize to z", unit: "µm", initial: 0 },
    { key: "removal_amount", label: "Removal amount", unit: "µm", initial: 0.05 },
  ],
  no_geometry: [
    { key: "time_min", label: "Time", unit: "min", initial: 1 },
    { key: "temperature_c", label: "Temperature", unit: "°C", initial: 25 },
  ],
  oxidation: [
    { key: "target", label: "Consumed thickness", unit: "µm", initial: 0.02 },
    { key: "time_min", label: "Time", unit: "min", initial: 1 },
    { key: "temperature_c", label: "Temperature", unit: "°C", initial: 900 },
  {
    key: "resolution",
    label: "Resolution (this step)",
    unit: "µm",
    initial: 0.01,
    hint: "Grid for this step only; leave it out to use the project's. Coarser is faster.",
  },
  ],
  flip: [
    {
      key: "axis",
      label: "Turned about",
      kind: "text",
      options: ["y", "x"],
      initial: "y",
      hint: "About y turns it left to right (x mirrors); about x, front to back.",
    },
  ],
};

/** Settings a tool is given that no kernel reads.
 *
 * The experiment set answers "what was the machine set to", so it holds the
 * knobs of the machine -- the recipe loaded on it, power, pressure, flow --
 * beside the process fields, which are often the same numbers. Anything
 * else can still be typed in as JSON, the way an imported field is.
 */
const TOOL_SETTINGS: ParameterSpec[] = [
  { key: "tool_recipe", label: "Tool recipe", kind: "text", initial: "" },
  { key: "power_w", label: "Power", unit: "W", initial: 100 },
  { key: "pressure_mtorr", label: "Pressure", unit: "mTorr", initial: 10 },
  { key: "gas_flow_sccm", label: "Gas flow", unit: "sccm", initial: 50 },
  { key: "notes", label: "Notes", kind: "text", initial: "" },
];

/** The rows the experiment set offers: the process fields, then the machine's.
 *
 * ``toolRecipes`` are the recipes loaded on the tool this step runs on, so
 * the Tool recipe row suggests them; anything else can still be typed, for
 * a machine whose recipe book is not in the library.
 */
export function experimentSpecs(type: ProcessType, toolRecipes: string[] = []): ParameterSpec[] {
  // the grid is the simulation's business, not the machine's
  const process = PROCESS_SPECS[type].filter((spec) => spec.key !== "resolution");
  const own = new Set(process.map((spec) => spec.key));
  const settings = TOOL_SETTINGS.filter((spec) => !own.has(spec.key)).map((spec) =>
    spec.key === "tool_recipe" && toolRecipes.length > 0
      ? { ...spec, options: toolRecipes, hint: `Loaded on this tool: ${toolRecipes.join(", ")}.` }
      : spec,
  );
  return [...process, ...settings];
}

/** What the simulation of each process type reads; nothing else is shown there.
 *
 * A deposition is its thickness and how it lands; an etch, its time and each
 * material's rate (or a target depth). Temperature, pressure, cycles, power
 * and the like change nothing the kernel builds, so they live in the
 * experiment record only.
 */
export const SIMULATION_KEYS: Record<ProcessType, string[]> = {
  deposit: ["target", "mode", "resolution"],
  etch: ["time_min", "target", "directional_fraction", "resolution"],
  cmp: ["target_z", "removal_amount"],
  no_geometry: [],
  oxidation: ["time_min", "target", "resolution"],
  flip: ["axis"],
};

const SIMULATION_HINTS: Record<string, string> = {
  "etch.time_min": "Each material is etched for this long at its rate below.",
  "etch.target": "Instead of a time: the depth the fastest material is etched to.",
  "oxidation.time_min": "Each material oxidises this long at its rate below.",
  "oxidation.target": "Instead of a time: how deep the fastest material is consumed.",
};

export const PARAMETER_SPECS: Record<ProcessType, ParameterSpec[]> = Object.fromEntries(
  (Object.keys(PROCESS_SPECS) as ProcessType[]).map((type) => [
    type,
    SIMULATION_KEYS[type].map((key) => {
      const spec = PROCESS_SPECS[type].find((item) => item.key === key) as ParameterSpec;
      const hint = SIMULATION_HINTS[`${type}.${key}`];
      return hint ? { ...spec, hint } : spec;
    }),
  ]),
) as Record<ProcessType, ParameterSpec[]>;

/** Fields a step may carry that the simulation view leaves out: the
 * experiment's (an old project kept both in one set), never the kernel's. */
export function simulationHiddenKeys(type: ProcessType): string[] {
  // only what the kernel never reads for this type: a field it does read
  // stays in sight, under its own name if it has no row
  const inert = ["temperature_c", ...TOOL_SETTINGS.map((spec) => spec.key)];
  const byType: Partial<Record<ProcessType, string[]>> = {
    // read only when there is no target thickness; the note under the rows says so
    deposit: ["cycles", "rate_per_cycle", "rate", "time_min"],
    no_geometry: ["time_min"],
  };
  return ["sketch_id", ...inert, ...(byType[type] ?? [])];
}

export function specFor(type: ProcessType, key: string): ParameterSpec | undefined {
  return PROCESS_SPECS[type].find((spec) => spec.key === key);
}

/** Whether a tool name or group names a cycle-by-cycle deposition.
 *
 * ALD is written in cycles everywhere it is used, and so is ALE; a tool
 * called "Savannah ALD" or grouped under "Deposition/ALD" is one of those.
 * The test is on the words, not on a field, because the tool library is the
 * user's own list of the machines in their lab.
 */
export function cyclic(tool: string | null | undefined): boolean {
  return /(^|[^a-z])(ald|ale|mld)([^a-z]|$)/i.test(tool ?? "");
}

const BY_TIME: Record<ProcessType, Record<string, ParameterValue>> = {
  deposit: { target: 0.05 },
  etch: { target: 0.1, directional_fraction: 1 },
  cmp: { target_z: 0 },
  no_geometry: {},
  oxidation: { target: 0.02 },
  flip: {},
};

const BY_CYCLE: Partial<Record<ProcessType, Record<string, ParameterValue>>> = {
  deposit: { cycles: 100, rate_per_cycle: 0.0001 },
};

/** The parameters a new step or recipe of this type starts with.
 *
 * A cyclic tool starts from the numbers its recipe is actually written in:
 * cycles and the rate per cycle, whose product is the thickness. Everything
 * else starts from the thickness or depth itself.
 */
export function defaultParameters(
  type: ProcessType,
  tool?: string | null,
): Record<string, ParameterValue> {
  const cyclicDefaults = cyclic(tool) ? BY_CYCLE[type] : undefined;
  return { ...(cyclicDefaults ?? BY_TIME[type]) };
}

function sameParameters(a: Record<string, ParameterValue>, b: Record<string, ParameterValue>) {
  const keys = Object.keys(a);
  return (
    keys.length === Object.keys(b).length && keys.every((key) => key in b && a[key] === b[key])
  );
}

/** Every set of parameters that means "nobody has typed here yet". */
function untouchedSets(): Record<string, ParameterValue>[] {
  return [...Object.values(BY_TIME), ...Object.values(BY_CYCLE)].filter(Boolean) as Record<
    string,
    ParameterValue
  >[];
}

/** The defaults for a new tool or type, while what is there is still a default.
 *
 * Choosing an ALD tool on a recipe nobody has typed into should leave the
 * parameters an ALD recipe has, and switching an etch to a deposition should
 * leave a thickness rather than an etch depth. Once a number has been
 * changed, added or removed it is the user's, and neither a tool nor a type
 * ever rewrites it -- which is why every default set counts as untouched,
 * not only this type's: the set on screen is the one the previous type or
 * tool put there.
 */
export function defaultsForNewTool(
  type: ProcessType,
  tool: string | null | undefined,
  parameters: Record<string, ParameterValue>,
): Record<string, ParameterValue> | null {
  const wanted = defaultParameters(type, tool);
  if (sameParameters(parameters, wanted)) return null;
  return untouchedSets().some((candidate) => sameParameters(parameters, candidate)) ? wanted : null;
}

/** The thickness a deposition works out to, and where it came from.
 *
 * The kernel reads a target thickness, else cycles × rate per cycle, else
 * time × rate. Showing the result is what makes the cycle form usable: 240
 * cycles at 0.09 nm is a number nobody should be multiplying by hand.
 */
export function depositThickness(
  parameters: Record<string, ParameterValue>,
): { um: number; from: "target" | "cycles" | "time" } | null {
  const scalar = (key: string): number | null => {
    const value = parameters[key];
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  };
  const target = scalar("target") ?? scalar("thickness");
  if (target !== null) return { um: target, from: "target" };
  const cycles = scalar("cycles");
  const perCycle = scalar("rate_per_cycle");
  if (cycles !== null && perCycle !== null) {
    return { um: Number((cycles * perCycle).toPrecision(12)), from: "cycles" };
  }
  const minutes = scalar("time_min");
  const rate = scalar("rate");
  if (minutes !== null && rate !== null) {
    return { um: Number((minutes * rate).toPrecision(12)), from: "time" };
  }
  return null;
}

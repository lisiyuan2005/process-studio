import { X } from "lucide-react";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { UnitNumber } from "./UnitNumber";
import type { ParameterSpec } from "../domain/parameters";
import type { ParameterValue } from "../types";

/** The parameter rows of a step or of a library recipe -- the same rows.
 *
 * A step and the recipe it was saved from hold the same fields, so they are
 * edited the same way: the parameters a process type understands, each in
 * the unit its owner prefers, plus whatever else the flow file or an import
 * put there, as JSON.
 */

const MODE_LABELS: Record<string, string> = {
  conformal: "Conformal",
  planar: "Planar",
  directional: "Directional prism",
  evaporation: "Evaporation",
  fill: "Fill",
};

function ParameterRow({
  spec,
  value,
  depositionModes,
  onChange,
  onRemove,
}: {
  spec: ParameterSpec;
  value: ParameterValue;
  depositionModes: string[];
  onChange: (value: ParameterValue) => void;
  onRemove: () => void;
}) {
  return (
    <div className="field-row parameter-row">
      <div className="parameter-heading">
        {spec.label}
        <button type="button" title={`Remove ${spec.label}`} onClick={onRemove}>
          <X size={11} />
        </button>
      </div>
      {spec.kind === "mode" ? (
        <select value={String(value)} onChange={(event) => onChange(event.target.value)}>
          {depositionModes.map((mode) => (
            <option key={mode} value={mode}>
              {MODE_LABELS[mode] ?? mode}
            </option>
          ))}
        </select>
      ) : spec.kind === "text" ? (
        <input value={String(value)} onChange={(event) => onChange(event.target.value)} />
      ) : (
        <UnitNumber
          field={spec.key}
          value={Array.isArray(value) ? value[0] : (value as number | null)}
          unit={spec.unit}
          onCommit={(next) => onChange(next)}
        />
      )}
      {spec.hint && <small>{spec.hint}</small>}
    </div>
  );
}

function UnknownParameterRow({
  name,
  value,
  onChange,
  onRemove,
}: {
  name: string;
  value: ParameterValue;
  onChange: (value: ParameterValue) => void;
  onRemove: () => void;
}) {
  const [draft, setDraft] = useState(JSON.stringify(value));
  const [error, setError] = useState(false);
  useEffect(() => setDraft(JSON.stringify(value)), [JSON.stringify(value)]);
  return (
    <div className="field-row parameter-row">
      <div className="parameter-heading">
        {name}
        <button type="button" title={`Remove ${name}`} onClick={onRemove}>
          <X size={11} />
        </button>
      </div>
      <input
        className={error ? "invalid-input" : ""}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => {
          try {
            onChange(JSON.parse(draft) as ParameterValue);
            setError(false);
          } catch {
            setError(true);
          }
        }}
      />
      {error && <small>Use JSON syntax: 1, true, "text", or [24,24,24].</small>}
    </div>
  );
}

interface ParameterEditorProps {
  /** The parameters this process type understands, already filtered. */
  specs: ParameterSpec[];
  parameters: Record<string, ParameterValue>;
  depositionModes: string[];
  /** A patch to apply; null or "" for a value removes it. */
  onPatch: (patch: Record<string, ParameterValue>) => void;
  /** Keys that are not parameters and are never shown (a sketch id). */
  hiddenKeys?: string[];
}

export function ParameterEditor({
  specs,
  parameters,
  depositionModes,
  onPatch,
  hiddenKeys = ["sketch_id"],
}: ParameterEditorProps) {
  const [toAdd, setToAdd] = useState("");
  const known = new Set(specs.map((spec) => spec.key));
  const active = specs.filter((spec) => spec.key in parameters);
  const missing = specs.filter((spec) => !(spec.key in parameters));
  const unknown = Object.entries(parameters).filter(
    ([key]) => !known.has(key) && !hiddenKeys.includes(key),
  );

  return (
    <>
      {active.map((spec) => (
        <ParameterRow
          key={spec.key}
          spec={spec}
          value={parameters[spec.key]}
          depositionModes={depositionModes}
          onChange={(value) => onPatch({ [spec.key]: value })}
          onRemove={() => onPatch({ [spec.key]: null })}
        />
      ))}
      {unknown.map(([key, value]) => (
        <UnknownParameterRow
          key={key}
          name={key}
          value={value}
          onChange={(next) => onPatch({ [key]: next })}
          onRemove={() => onPatch({ [key]: null })}
        />
      ))}
      {missing.length > 0 && (
        <div className="add-parameter-row">
          <select value={toAdd} onChange={(event) => setToAdd(event.target.value)}>
            <option value="">Add a parameter…</option>
            {missing.map((spec) => (
              <option key={spec.key} value={spec.key}>
                {spec.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={!toAdd}
            onClick={() => {
              const spec = missing.find((item) => item.key === toAdd);
              if (spec) onPatch({ [spec.key]: spec.initial });
              setToAdd("");
            }}
          >
            Add
          </button>
        </div>
      )}
    </>
  );
}

interface ParameterSetsProps {
  /** What the kernel reads. */
  specs: ParameterSpec[];
  parameters: Record<string, ParameterValue>;
  /** What the tool was set to; null means the same as `parameters`. */
  experimentSpecs: ParameterSpec[];
  experiment: Record<string, ParameterValue> | null | undefined;
  depositionModes: string[];
  onPatch: (patch: Record<string, ParameterValue>) => void;
  /** A patch for the experiment set; null goes back to "the same". */
  onExperimentPatch: (patch: Record<string, ParameterValue> | null) => void;
  /** Shown under the simulation rows (the thickness a deposition works out to). */
  footer?: ReactNode;
}

/**
 * The two sets of numbers a step or a recipe carries.
 *
 * The simulation set is what the kernel is asked to build; the experiment
 * set is what the tool is actually set to. They are usually the same, so
 * the second one says so and holds nothing until somebody says otherwise --
 * at which point it starts as a copy, and only the differences are typed.
 * Nothing in the experiment set reaches a kernel or a digest.
 */
export function ParameterSets({
  specs,
  parameters,
  experimentSpecs,
  experiment,
  depositionModes,
  onPatch,
  onExperimentPatch,
  footer,
}: ParameterSetsProps) {
  const [tab, setTab] = useState<"simulation" | "experiment">("simulation");
  const separate = experiment !== null && experiment !== undefined;

  return (
    <>
      <div className="set-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "simulation"}
          className={tab === "simulation" ? "active" : ""}
          onClick={() => setTab("simulation")}
        >
          Simulation
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "experiment"}
          className={tab === "experiment" ? "active" : ""}
          onClick={() => setTab("experiment")}
        >
          Experiment{separate ? "" : " ="}
        </button>
      </div>

      {tab === "simulation" ? (
        <>
          <ParameterEditor
            specs={specs}
            parameters={parameters}
            depositionModes={depositionModes}
            onPatch={onPatch}
          />
          {footer}
        </>
      ) : separate ? (
        <>
          <ParameterEditor
            specs={experimentSpecs}
            parameters={experiment}
            depositionModes={depositionModes}
            onPatch={(patch) => onExperimentPatch(patch)}
          />
          <p className="numerics-note">
            What the tool was set to. The kernel reads none of it, so editing here never makes a
            result out of date.
          </p>
          <button type="button" className="secondary-button" onClick={() => onExperimentPatch(null)}>
            Use the simulation values
          </button>
        </>
      ) : (
        <>
          <p className="numerics-note">
            The tool was set to the same values as the simulation. Give this its own settings to
            record what actually ran — the recipe on the machine, a time, a power — without
            changing what is built.
          </p>
          <button type="button" className="secondary-button" onClick={() => onExperimentPatch({})}>
            Record separate experiment values
          </button>
        </>
      )}
    </>
  );
}

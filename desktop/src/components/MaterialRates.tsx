import { Trash2 } from "lucide-react";
import type { MaterialResponse } from "../types";
import { UnitNumber } from "./UnitNumber";

/**
 * Each material's rate in an etch (or an oxidation), with its stop box.
 *
 * The rate is stored in µm/min, the unit the kernel reads, and typed in
 * whichever unit is picked beside it -- nm/s until another is chosen, the
 * way etch rates are usually quoted. The choice is remembered for every
 * rate field.
 */
export function MaterialRates({
  responses,
  rateLabel,
  onChange,
  onRemove,
}: {
  responses: MaterialResponse[];
  rateLabel: string;
  onChange: (material: string, patch: Partial<MaterialResponse>) => void;
  onRemove: (material: string) => void;
}) {
  if (responses.length === 0) return null;
  return (
    <>
      <div className="response-row response-head" aria-hidden="true">
        <span>Material</span>
        <span>{rateLabel}</span>
        <span>Stop</span>
        <span />
      </div>
      {responses.map((response) => (
        <div className="response-row" key={response.material}>
          <input type="text" value={response.material} readOnly aria-label="Material" />
          <UnitNumber
            field="material_rate"
            unit="µm/min"
            defaultUnit="nm/s"
            aria-label={`${rateLabel} of ${response.material}`}
            value={response.rateUmPerMin}
            onCommit={(rateUmPerMin) => onChange(response.material, { rateUmPerMin: Math.max(0, rateUmPerMin) })}
          />
          <label className="response-stop" title="A stop layer: this material is not etched (a zero rate)">
            <input
              type="checkbox"
              aria-label={`${response.material} is a stop layer`}
              checked={response.stopLayer}
              onChange={(event) => onChange(response.material, { stopLayer: event.target.checked })}
            />
          </label>
          <button type="button" aria-label={`Remove ${response.material}`} onClick={() => onRemove(response.material)}>
            <Trash2 size={12} />
          </button>
        </div>
      ))}
    </>
  );
}

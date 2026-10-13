import type { QuickSketch, SketchShape } from "../types";

/**
 * A new sketch that starts as a copy of ``source``: its own id, a name no
 * other sketch has ("Trench copy", then "Trench copy 2", ...) and shapes of
 * its own, so editing it leaves the source and every step using it alone.
 */
export function sketchCopy(source: QuickSketch, others: readonly QuickSketch[], id: string): QuickSketch {
  const taken = new Set(others.map((item) => item.name));
  const base = `${source.name} copy`;
  let name = base;
  for (let n = 2; taken.has(name); n += 1) name = `${base} ${n}`;
  return { id, name, shapes: structuredClone(source.shapes) };
}

/** How wide and tall one copy of a shape is, in micrometres. */
export interface Footprint {
  width: number;
  height: number;
}

function number(value: unknown, fallback = 0): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function points(value: unknown): [number, number][] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item) => Array.isArray(item) && item.length === 2)
    .map((item) => [number(item[0]), number(item[1])] as [number, number]);
}

/** The bounding size of a single copy of ``shape``, before the array. */
export function footprint(shape: SketchShape): Footprint {
  const parameters = shape.parameters;
  if (shape.kind === "rectangle") {
    const [size] = points([parameters.size ?? [0, 0]]);
    return { width: Math.abs(size?.[0] ?? 0), height: Math.abs(size?.[1] ?? 0) };
  }
  if (shape.kind === "circle") {
    const diameter = Math.abs(number(parameters.radius)) * 2;
    return { width: diameter, height: diameter };
  }
  const vertices = points(parameters.points);
  if (vertices.length === 0) return { width: 0, height: 0 };
  const xs = vertices.map(([x]) => x);
  const ys = vertices.map(([, y]) => y);
  // A path is a stroke, so its own width thickens the outline on both sides;
  // a zero-area polyline is still that wide on the wafer.
  const stroke = shape.kind === "path" ? Math.abs(number(parameters.width)) : 0;
  return {
    width: Math.max(...xs) - Math.min(...xs) + stroke,
    height: Math.max(...ys) - Math.min(...ys) + stroke,
  };
}

/** Which axes of an array place copies closer together than they are wide.
 *
 * Nothing is wrong with it -- the copies are unioned, so a pitch under the
 * feature size is how a comb or a long trench is drawn out of one shape --
 * but it is also what a mistyped pitch looks like, and the picture alone
 * does not always show it (two merged circles read as a slot). So the
 * editor says it out loud and leaves the decision alone.
 */
export function crowdedAxes(shape: SketchShape): { x: boolean; y: boolean } {
  const [countX, countY, pitchX, pitchY] = shape.array;
  const size = footprint(shape);
  return {
    x: countX > 1 && size.width > 0 && Math.abs(pitchX) < size.width,
    y: countY > 1 && size.height > 0 && Math.abs(pitchY) < size.height,
  };
}

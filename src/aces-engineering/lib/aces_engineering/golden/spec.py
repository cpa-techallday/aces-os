"""aces golden: the input spec `aces-golden-spec/1`, its validation, and everything derived from it.

A spec is JSON validated by pydantic v2 models that refuse unknown keys. `resolve` applies the command-line overrides
(dotted keys), validates, and returns the effective spec: the validated spec as JSON plus `derived`, the resolutions,
variables, outputs and limits every later stage reads. `guard` refuses specs the grids cannot resolve (exit 4) and
`check_geometry` refuses boundary conditions and regions that do not sit on the grids (exit 2). Frames and grids follow
section 5 of the build spec: boxes are in the part frame (the STEP's own frame, mm); element (i, j, k) at resolution h
has its centre at origin + (i + 0.5, j + 0.5, k + 0.5) * h.

No numpy here: importing this module must stay quick."""
from __future__ import annotations

import copy
import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (AfterValidator, BaseModel, ConfigDict, Field, NonNegativeFloat, PositiveFloat, ValidationError,
                      field_validator, model_validator)

from . import EXIT_USAGE, GoldenError

SPEC_SCHEMA = "aces-golden-spec/1"
AXES = "xyz"
CHECK_CELL_LIMIT = 2_000_000          # cells at fea_mm above which a spec is refused
CPU_CELL_WARNING = 200_000            # cells at fea_mm above which a CPU-only host is warned
_TOL = 1e-6                           # mm, for "inside the envelope" and planar tests
_LOC_TAGS = {"float", "int", "str", "bool", "list", "dict", "tuple", "[key]"}


class SpecError(ValueError):
    """A spec that cannot be used as given: unreadable, not JSON, or failing validation. The message is one line
    naming the location of the first problem; the pipeline maps it to GoldenError(EXIT_USAGE)."""


# ----------------------------------------------------------------------------------------------------------- models

def _box6(v: list[float]) -> list[float]:
    if len(v) != 6:
        raise ValueError(f"a box is 6 numbers x0,y0,z0,x1,y1,z1 in mm, not {len(v)}")
    for a in range(3):
        if v[a] > v[a + 3]:
            raise ValueError(f"box {AXES[a]}0 {v[a]:g} is greater than {AXES[a]}1 {v[a + 3]:g}")
    return [float(x) for x in v]


Box6 = Annotated[list[float], AfterValidator(_box6)]
OutputName = Literal["mass_kg", "mass_voxel_kg", "mesh_volume_mm3", "voxel_volume_mm3", "compliance_Nmm",
                     "umax_mm", "vm_max_MPa", "vm_p99_MPa", "vm_p95_MPa"]
VarName = Literal["volfrac", "rmin_mm", "penal", "shell_mm", "cell_mm", "wall_mm"]
LATTICE_VARS = ("shell_mm", "cell_mm", "wall_mm")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, allow_inf_nan=False)


class Cyl(_Model):
    """A cylinder along `axis`; `center` holds the other two axes in order (x: y,z; y: x,z; z: x,y)."""
    axis: Literal["x", "y", "z"]
    center: tuple[float, float]
    r: PositiveFloat
    span: tuple[float, float]

    @model_validator(mode="after")
    def _span_increases(self) -> "Cyl":
        if not self.span[0] < self.span[1]:
            raise ValueError(f"cylinder span {self.span[0]:g}..{self.span[1]:g} must increase")
        return self


class Region(_Model):
    """A keep-in (preserved solid) or keep-out (preserved void): exactly one of box or cyl, optionally rounded."""
    name: str = ""
    box: Box6 | None = None
    cyl: Cyl | None = None
    round_mm: NonNegativeFloat = 0.0

    @model_validator(mode="after")
    def _one_shape(self) -> "Region":
        if (self.box is None) == (self.cyl is None):
            raise ValueError("a region is exactly one of box or cyl")
        if self.round_mm > 0:
            if self.box is not None:
                ext = min(self.box[a + 3] - self.box[a] for a in range(3))
            else:
                ext = min(2.0 * self.cyl.r, self.cyl.span[1] - self.cyl.span[0])
            if not self.round_mm < 0.5 * ext:
                raise ValueError(f"round_mm {self.round_mm:g} must be under half the smallest extent ({0.5 * ext:g} mm)")
        return self


class Support(_Model):
    name: str = ""
    box: Box6
    dofs: list[int] = Field(default_factory=lambda: [0, 1, 2])

    @field_validator("dofs")
    @classmethod
    def _dofs(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("dofs is empty: fix at least one of 0 (x), 1 (y), 2 (z)")
        if len(set(v)) != len(v) or any(d not in (0, 1, 2) for d in v):
            raise ValueError(f"dofs {v} must be unique values from 0 (x), 1 (y), 2 (z)")
        return v


class Load(_Model):
    name: str = ""
    box: Box6
    force: tuple[float, float, float]
    axis: Literal["x", "y", "z"] | None = None
    case: str = "default"

    @model_validator(mode="after")
    def _force_and_traction(self) -> "Load":
        if all(f == 0.0 for f in self.force):
            raise ValueError("force is zero on every axis")
        if self.axis is not None:
            a = AXES.index(self.axis)
            if abs(self.box[a + 3] - self.box[a]) > _TOL:
                raise ValueError(f"a traction load (axis {self.axis}) needs a box planar on {self.axis}: "
                                 f"{self.axis}0 {self.box[a]:g} != {self.axis}1 {self.box[a + 3]:g}")
        return self


class B123d(_Model):
    """A build123d design-space builder: "pkg.module:func" or "file.py:func" (relative to the spec file)."""
    ref: str
    kwargs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ref")
    @classmethod
    def _ref(cls, v: str) -> str:
        mod, sep, func = v.rpartition(":")
        if not sep or not mod or not func.isidentifier():
            raise ValueError(f"ref {v!r} must be 'pkg.module:func' or 'file.py:func'")
        return v


class DomainSpec(_Model):
    source: Literal["step", "build123d"]
    step_file: str | None = None
    build123d: B123d | None = None
    origin_mm: tuple[float, float, float] | None = None
    envelope_mm: tuple[PositiveFloat, PositiveFloat, PositiveFloat] | None = None
    margin_mm: NonNegativeFloat = 0.0
    keep_in: list[Region] = Field(default_factory=list)
    keep_out: list[Region] = Field(default_factory=list)
    patch_keep_in_mm: NonNegativeFloat | None = None
    clip_to_domain: bool = True

    @model_validator(mode="after")
    def _source(self) -> "DomainSpec":
        if self.source == "step" and not self.step_file:
            raise ValueError("step_file is required when source is step")
        if self.source == "build123d" and self.build123d is None:
            raise ValueError("build123d (ref and kwargs) is required when source is build123d")
        if (self.origin_mm is None) != (self.envelope_mm is None):
            raise ValueError("give both origin_mm and envelope_mm, or neither (the grid is then derived from the STEP)")
        return self


class Material(_Model):
    name: str
    E_MPa: PositiveFloat
    nu: float = Field(gt=0.0, lt=0.5)
    rho_kg_mm3: PositiveFloat
    allowable_vm_MPa: PositiveFloat | None = None
    source: str

    @field_validator("source")
    @classmethod
    def _sourced(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("material source is empty: say where E, nu, density and the allowable come from")
        return v


class Objective(_Model):
    output: OutputName = "mass_kg"
    sense: Literal["min", "max"] = "min"


class Constraint(_Model):
    output: OutputName
    op: Literal["<=", ">="]
    limit: PositiveFloat | Literal["allowable"]


class Bound(_Model):
    lo: float
    hi: float

    @model_validator(mode="after")
    def _increases(self) -> "Bound":
        if not self.lo < self.hi:
            raise ValueError(f"bound lo {self.lo:g} must be below hi {self.hi:g}")
        return self


class Skin(_Model):
    upsample: int = Field(2, ge=1, le=4)
    sigma_vox: float = Field(1.0, ge=0.0, le=3.0)
    blend_mm: NonNegativeFloat = 5.0
    clip_blend_mm: NonNegativeFloat = 2.0
    taubin_iters: int = Field(5, ge=0, le=50)
    iso: float = Field(0.5, ge=0.05, le=0.95)


class Infill(_Model):
    kind: Literal["gyroid"]
    blend_mm: NonNegativeFloat = 1.5


class Generator(_Model):
    kind: Literal["simp-organic"] = "simp-organic"
    variables: dict[VarName, Bound]
    fixed: dict[VarName, float] = Field(default_factory=dict)
    iterations: int = Field(25, ge=5, le=200)
    change_tol: PositiveFloat = 0.01
    move: float = Field(0.2, gt=0.0, le=1.0)
    solver_tol: PositiveFloat = 1e-4
    skin: Skin = Field(default_factory=Skin)
    infill: Infill | None = None

    @model_validator(mode="after")
    def _variables(self) -> "Generator":
        if not self.variables:
            raise ValueError("variables is empty: name at least one design variable with lo and hi")
        both = [k for k in self.variables if k in self.fixed]
        if both:
            raise ValueError(f"{both[0]} is in both variables and fixed: give it once")
        for need in ("volfrac", "rmin_mm"):
            if need not in self.variables and need not in self.fixed:
                raise ValueError(f"{need} must be in variables or fixed")
        for k in LATTICE_VARS:
            given = k in self.variables or k in self.fixed
            if self.infill is None and given:
                raise ValueError(f"{k} needs an infill (add \"infill\": {{\"kind\": \"gyroid\"}})")
            if self.infill is not None and not given:
                raise ValueError(f"the gyroid infill needs {k} in variables or fixed")
        return self


class Resolution(_Model):
    gen_mm: PositiveFloat
    fea_mm: PositiveFloat | None = None
    check_factor: float = Field(0.5, gt=0.0, lt=1.0)
    stress_exclude_mm: NonNegativeFloat | None = None


class SweepCfg(_Model):
    n: int = Field(10, ge=2, le=64)
    seed: int = Field(0, ge=0, le=2**32 - 1)  # the LHS sampler and the GP fit both need a 32-bit unsigned seed


class SurrogateCfg(_Model):
    kind: Literal["gp", "et"] = "gp"
    folds: int = Field(5, ge=2, le=10)
    min_points: int = Field(8, ge=3)
    r2_flag: float = 0.9
    log_outputs: Literal["auto-positive", "auto", "never"] = "auto-positive"


class OptimiseCfg(_Model):
    method: Literal["auto", "grid", "lhs", "optuna"] = "auto"
    candidates: int = Field(4096, ge=64)
    trials: int = Field(400, ge=16)
    kappa: NonNegativeFloat = 1.0
    cv_margin: bool = True
    rounds: int = Field(3, ge=1, le=5)


class VerifyCfg(_Model):
    tol: float = Field(0.10, gt=0.0, lt=1.0)
    check_objective: bool = True
    check: Literal["auto", "off", "force"] = "auto"
    check_budget_s: PositiveFloat = 60.0


class SolverCfg(_Model):
    dtype: Literal["float64"] = "float64"
    tol: PositiveFloat = 1e-8
    maxiter: int = Field(20000, ge=100)


class Flow(_Model):
    """Optional Tier-1 CFD block (`flow:`): template a PyFR ac-navier-stokes case on the exported part and queue it
    on the rig. Gated honestly, because the engine and the mesh path bound what the numbers can mean:

    - ac-navier-stokes is incompressible: air faster than 100 m/s (~Mach 0.3) is refused — an incompressible drag
      number there is not a number you can engineer to. Water carries a no-cavitation note instead.
    - the gmsh mesh stage names inlet/outlet by the box's x faces, so the flow is +x over the part as exported;
      a part that must see another heading is rotated before export, not here.
    - the body is the faceted STEP (mesh-derived, not B-rep): wall curvature is the verify mesh's.
    """
    velocity_mps: PositiveFloat
    medium: Literal["air", "water"] = "air"
    nu_m2s: PositiveFloat | None = None      # kinematic viscosity override; default from `medium`
    rho_kgm3: PositiveFloat | None = None    # density override (SI force recovery only; Cd is rho-free)
    reference_length_m: PositiveFloat | None = None   # default: part bbox extent along +x
    reference_area_m2: PositiveFloat | None = None    # default: bbox frontal area (an approximation, labelled)
    farfield: tuple[PositiveFloat, PositiveFloat, PositiveFloat] = (3.0, 9.0, 3.0)  # upstream, downstream, lateral, x D
    lc_body_m: PositiveFloat | None = None   # wall cell size; default min(resolution.fea_mm, reference_length / 8)
    lc_far_m: PositiveFloat | None = None    # farfield cell size; default reference_length
    dist_max_m: PositiveFloat | None = None  # refinement halo; default 2 x reference_length
    order: int = Field(2, ge=1, le=5)
    precision: Literal["single", "double"] = "single"
    tend_s: PositiveFloat | None = None      # default 20 reference-length flow-throughs
    dt_s: PositiveFloat | None = None        # default lc_body / (12 U), the validated sphere smoke's ratio

    @model_validator(mode="after")
    def _incompressible(self) -> "Flow":
        if self.medium == "air" and self.velocity_mps > 100.0:
            raise ValueError(f"flow.velocity_mps: {self.velocity_mps:g} m/s in air is Mach "
                             f"~{self.velocity_mps / 343.0:.2f}; ac-navier-stokes is incompressible, so a drag "
                             "number past ~100 m/s (Mach 0.3) would be fiction — this tier refuses it")
        return self


class GoldenSpec(_Model):
    schema_id: Literal["aces-golden-spec/1"] = Field(alias="schema")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")
    description: str = ""
    units: Literal["mm-N-MPa-kg"] = "mm-N-MPa-kg"
    hypothesis: str | None = None
    domain: DomainSpec
    supports: list[Support] = Field(min_length=1)
    loads: list[Load] = Field(min_length=1)
    material: Material
    objective: Objective = Field(default_factory=Objective)
    constraints: list[Constraint] = Field(min_length=1)
    generator: Generator
    resolution: Resolution
    sweep: SweepCfg = Field(default_factory=SweepCfg)
    surrogate: SurrogateCfg = Field(default_factory=SurrogateCfg)
    optimise: OptimiseCfg = Field(default_factory=OptimiseCfg)
    verify: VerifyCfg = Field(default_factory=VerifyCfg)
    solver: SolverCfg = Field(default_factory=SolverCfg)
    flow: Flow | None = None    # optional Tier-1 CFD: a PyFR case on the exported part, queued on the rig

    @model_validator(mode="after")
    def _limits(self) -> "GoldenSpec":
        # Root-level messages carry their own location ("constraints[0].limit: ..."); see _first_error.
        seen: dict[tuple[str, str], int] = {}
        for i, c in enumerate(self.constraints):
            if c.limit == "allowable":
                if not c.output.startswith("vm_"):
                    raise ValueError(f"constraints[{i}].limit: 'allowable' applies to a von Mises output (vm_*), "
                                     f"not {c.output}")
                if self.material.allowable_vm_MPa is None:
                    raise ValueError(f"constraints[{i}].limit: 'allowable' needs material.allowable_vm_MPa")
            key = (c.output, c.op)
            if key in seen:
                raise ValueError(f"constraints[{i}]: {c.output} {c.op} is already constrained by constraints[{seen[key]}]; "
                                 "keep one limit")
            seen[key] = i
        return self

    @model_validator(mode="after")
    def _one_load_case(self) -> "GoldenSpec":
        # The vendored evaluator keeps the fields (vm_p99, vm_p95) of the case with the highest vm_max only, so a second
        # case could hide a violated stress constraint: v0 solves one load case per spec.
        first = self.loads[0].case
        for i, ld in enumerate(self.loads):
            if ld.case != first:
                raise ValueError(f"loads[{i}].case: v0 solves one load case; run one spec per case (loads[0] is case "
                                 f"{first!r}, loads[{i}] is case {ld.case!r})")
        return self


# ------------------------------------------------------------------------------------------------------- load, resolve

def load(path: Path, *, step: Path | None = None) -> tuple[dict, bytes, Path]:
    """Read a JSON spec. Returns (raw dict, the file's bytes, base_dir = the spec file's directory). `step` replaces
    the design space with that STEP file: domain.source -> "step", domain.step_file -> its absolute path."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SpecError(f"cannot read the spec {path}: {exc.strerror or exc}") from None
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise SpecError(f"the spec {path} is not UTF-8 text ({exc.reason})") from None
    except json.JSONDecodeError as exc:
        raise SpecError(f"the spec {path} is not valid JSON: line {exc.lineno} column {exc.colno}: {exc.msg}") from None
    if not isinstance(raw, dict):
        raise SpecError(f"the spec {path} must be a JSON object, not {type(raw).__name__}")
    if step is not None:
        dom = raw.get("domain")
        if not isinstance(dom, dict):
            dom = {}
            raw["domain"] = dom
        dom["source"] = "step"
        dom["step_file"] = str(Path(step).resolve())
        dom.pop("build123d", None)
    return raw, data, path.resolve().parent


def _set_dotted(data: dict, key: str, value: Any) -> None:
    parts = [p for p in str(key).split(".") if p]
    if not parts:
        raise SpecError(f"override {key!r} names no spec key")
    node = data
    for i, p in enumerate(parts[:-1]):
        nxt = node.get(p)
        if nxt is None:
            nxt = {}
            node[p] = nxt
        if not isinstance(nxt, dict):
            raise SpecError(f"override {key}: {'.'.join(parts[:i + 1])} is not an object in the spec")
        node = nxt
    node[parts[-1]] = value


def _where(loc: tuple) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
            continue
        s = str(part)
        if s in _LOC_TAGS or "[" in s or "'" in s or "-" in s:
            continue  # pydantic union-member and dict-key markers, not spec keys
        out += ("." if out else "") + s
    return out


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    msg = str(err.get("msg", "invalid"))
    for prefix in ("Value error, ", "Assertion failed, "):
        if msg.startswith(prefix):
            msg = msg[len(prefix):]
    where = _where(tuple(err.get("loc") or ()))
    if not where:
        head, sep, rest = msg.partition(": ")
        if sep and " " not in head:
            return f"spec error at {head}: {rest}"
        return f"spec error at (top level): {msg}"
    return f"spec error at {where}: {msg}"


def _domain_ref(dom: dict, base_dir: Path) -> str:
    if dom["source"] == "step":
        p = Path(dom["step_file"])
        if not p.is_absolute():
            p = base_dir / p
        return str(p.resolve())
    ref = dom["build123d"]["ref"]
    mod, _, func = ref.rpartition(":")
    if mod.endswith(".py"):
        p = Path(mod)
        if not p.is_absolute():
            p = base_dir / p
        return f"{p.resolve()}:{func}"
    return ref


def resolve(raw: dict, *, base_dir: Path, overrides: dict, gpu_usable: bool) -> dict:
    """Overrides (dotted keys) applied to a deep copy of `raw`, validated, and returned as the effective spec:
    `GoldenSpec.model_dump(mode="json", by_alias=True)` plus `derived`. Raises SpecError naming the first problem."""
    if not isinstance(raw, dict):
        raise SpecError("spec error at (top level): the spec must be a JSON object")
    data = copy.deepcopy(raw)
    applied: dict[str, Any] = {}
    for key, value in (overrides or {}).items():
        _set_dotted(data, key, value)
        applied[str(key)] = value
    try:
        spec = GoldenSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecError(_first_error(exc)) from None
    eff = spec.model_dump(mode="json", by_alias=True)
    base = Path(base_dir).resolve()
    res, gen_cfg = eff["resolution"], eff["generator"]
    gen = float(res["gen_mm"])
    fea = float(res["fea_mm"] if res["fea_mm"] is not None else gen)
    up = int(gen_cfg["skin"]["upsample"])
    cf = float(res["check_factor"])
    levels = [{"level": 1, "h_mm": fea * cf, "gpu_only": False}]
    if gpu_usable:
        levels.append({"level": 2, "h_mm": fea * cf ** 2, "gpu_only": True})
    variables = {k: [float(b["lo"]), float(b["hi"])] for k, b in gen_cfg["variables"].items()}
    fixed = {k: float(v) for k, v in gen_cfg["fixed"].items()}
    if "penal" not in variables and "penal" not in fixed:
        fixed["penal"] = 3.0
    allowable = eff["material"]["allowable_vm_MPa"]
    constraints = [{"output": c["output"], "op": c["op"],
                    "limit": float(allowable) if c["limit"] == "allowable" else float(c["limit"])}
                   for c in eff["constraints"]]
    outputs: list[str] = []
    for o in [eff["objective"]["output"]] + [c["output"] for c in constraints] + ["compliance_Nmm", "umax_mm", "vm_max_MPa"]:
        if o not in outputs:
            outputs.append(o)
    dom = eff["domain"]
    grid = None
    if dom["origin_mm"] is not None and dom["envelope_mm"] is not None:
        grid = {"origin_mm": [float(v) for v in dom["origin_mm"]], "envelope_mm": [float(v) for v in dom["envelope_mm"]],
                "derived_from": "spec"}
    eff["derived"] = {
        "gen_mm": gen, "fea_mm": fea, "field_h_mm": gen / up, "check_factor": cf, "check_levels": levels,
        "stress_exclude_mm": float(res["stress_exclude_mm"]) if res["stress_exclude_mm"] is not None else 1.5 * fea,
        "patch_keep_in_mm": float(dom["patch_keep_in_mm"]) if dom["patch_keep_in_mm"] is not None else 2.0 * gen,
        "variables": variables, "fixed": fixed, "outputs": outputs, "constraints": constraints,
        "objective": {"output": eff["objective"]["output"], "sense": eff["objective"]["sense"]},
        "seed": int(eff["sweep"]["seed"]), "n": int(eff["sweep"]["n"]), "tol": float(eff["verify"]["tol"]),
        "rounds": int(eff["optimise"]["rounds"]), "kappa": float(eff["optimise"]["kappa"]),
        "check_mode": eff["verify"]["check"], "check_budget_s": float(eff["verify"]["check_budget_s"]),
        "grid": grid, "domain_ref": _domain_ref(dom, base), "base_dir": str(base), "overrides": applied,
    }
    return eff


# ------------------------------------------------------------------------------------------------------------ guards

def _range(eff: dict, name: str) -> tuple[float, float] | None:
    d = eff["derived"]
    if name in d["variables"]:
        lo, hi = d["variables"][name]
        return float(lo), float(hi)
    if name in d["fixed"]:
        v = float(d["fixed"][name])
        return v, v
    return None


def _ge(a: float, b: float) -> bool:
    return a >= b - 1e-9 * max(1.0, abs(b))


def guard(eff: dict) -> list[str]:
    """The resolution guard: one line per refusal, each naming the value and the fix. A non-empty list means the
    pipeline exits EXIT_UNDER_RESOLVED before computing anything."""
    d = eff["derived"]
    gen, fea, field_h = d["gen_mm"], d["fea_mm"], d["field_h_mm"]
    up = eff["generator"]["skin"]["upsample"]
    out: list[str] = []
    rmin = _range(eff, "rmin_mm")
    if rmin is not None and not _ge(rmin[0], 1.5 * gen):
        out.append(f"rmin_mm {rmin[0]:g} mm is under 1.5 x gen_mm = {1.5 * gen:g} mm, so the density filter spans less "
                   f"than 1.5 cells: raise rmin_mm to at least {1.5 * gen:g} mm or use a finer --gen-res")
    if not field_h <= fea + 1e-9 * fea:
        need = int(math.ceil(gen / fea - 1e-9))
        fix = (f"raise generator.skin.upsample to {need}" if need <= 4 else "use a finer --gen-res")
        out.append(f"the skin field spacing {field_h:g} mm (gen_mm {gen:g} / upsample {up}) is coarser than the solve "
                   f"resolution fea_mm {fea:g} mm: {fix} or use a coarser --res")
    if eff["generator"]["infill"] is not None:
        wall, shell, cell = _range(eff, "wall_mm"), _range(eff, "shell_mm"), _range(eff, "cell_mm")
        if wall is not None and not _ge(wall[0], 2 * fea):
            out.append(f"gyroid wall_mm {wall[0]:g} mm is under 2 x fea_mm = {2 * fea:g} mm, so the solve cannot resolve "
                       f"the walls: raise wall_mm to at least {2 * fea:g} mm or use a finer --res")
        if wall is not None and not _ge(wall[0], 3 * field_h):
            out.append(f"gyroid wall_mm {wall[0]:g} mm is under 3 x the skin field spacing = {3 * field_h:g} mm: raise "
                       f"wall_mm to at least {3 * field_h:g} mm or raise generator.skin.upsample")
        if shell is not None and not _ge(shell[0], 2 * fea):
            out.append(f"shell_mm {shell[0]:g} mm is under 2 x fea_mm = {2 * fea:g} mm, so the solve cannot resolve the "
                       f"shell: raise shell_mm to at least {2 * fea:g} mm or use a finer --res")
        if cell is not None and wall is not None and not _ge(cell[0], 3 * wall[1]):
            out.append(f"gyroid cell_mm {cell[0]:g} mm is under 3 x wall_mm {wall[1]:g} mm = {3 * wall[1]:g} mm, so the "
                       f"walls would close the cells: raise cell_mm to at least {3 * wall[1]:g} mm or thin the walls")
    vf = _range(eff, "volfrac")
    if vf is not None and not (vf[0] > 0.02 and vf[1] < 0.9):
        out.append(f"volfrac {vf[0]:g}..{vf[1]:g} must lie inside (0.02, 0.9) of the design space: SIMP below 2 % has "
                   "nothing to connect and above 90 % is the design space itself")
    pe = _range(eff, "penal")
    if pe is not None and not (pe[0] >= 1.0 and pe[1] <= 5.0):
        out.append(f"penal {pe[0]:g}..{pe[1]:g} must lie inside [1, 5] (3 is the usual SIMP penalty)")
    return out


def _cells(env: list[float], h: float) -> list[int]:
    return [max(1, int(round(float(L) / h))) for L in env]


def _region_bbox(r: dict) -> list[float]:
    if r.get("box") is not None:
        return [float(v) for v in r["box"]]
    c = r["cyl"]
    a = AXES.index(c["axis"])
    o = [i for i in range(3) if i != a]
    lo, hi = [0.0] * 3, [0.0] * 3
    lo[a], hi[a] = float(c["span"][0]), float(c["span"][1])
    for k, ax in enumerate(o):
        lo[ax], hi[ax] = float(c["center"][k]) - float(c["r"]), float(c["center"][k]) + float(c["r"])
    return lo + hi


def _label(kind: str, item: dict, i: int) -> str:
    name = item.get("name") or f"{kind}{i + 1}"
    return f"{kind} '{name}'"


def _on_lattice(v: float, origin: float, h: float) -> bool:
    k = (v - origin) / h
    return abs(k - round(k)) <= 1e-6


def _snap(v: float, origin: float, h: float) -> tuple[float, float]:
    k = (v - origin) / h
    return origin + math.floor(k + 1e-9) * h, origin + math.ceil(k - 1e-9) * h


def _fmt(v: float) -> str:
    return f"{round(v, 6):g}"


def _and(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def check_hs(d: dict) -> list[tuple[float, str]]:
    """The planned independent-check resolutions as (h_mm, label): level 1 always, level 2 when a usable GPU planned it
    (eff["derived"]["check_levels"])."""
    return [(float(lv["h_mm"]), f"the level-{lv['level']} check") for lv in d.get("check_levels") or []]


def bc_lattice(eff: dict) -> tuple[float, list[tuple[float, str]]]:
    """The grid every support and load box face must sit on: the grid quantum of gen_mm (the SIMP generator), fea_mm (the
    sweep and verify solves) and every planned check level. On it, every resolution fixes and loads the same physical
    patch; off it, the vendored nodes_in_box and elems_in_box pick a different patch at each resolution. Returns
    (quantum, [(h, label), ...]); GoldenError(EXIT_USAGE) when the resolutions share no grid below 1000 mm."""
    d = eff["derived"]
    hs = [(float(d["gen_mm"]), "gen_mm"), (float(d["fea_mm"]), "fea_mm")] + check_hs(d)
    return grid_quantum([h for h, _ in hs]), hs


def stress_lattice(eff: dict) -> float:
    """The grid the stress-exclusion zones snap outward to: the grid quantum of fea_mm and every planned check level
    (fea_mm itself for the usual check factor 1/2), so every solve resolution excludes the same physical zone."""
    d = eff["derived"]
    return grid_quantum([float(d["fea_mm"])] + [h for h, _ in check_hs(d)])


def _near_box(box: list[float], origin: list[float], env: list[float], q: float) -> list[float]:
    """The nearest box with every face on the q grid inside the envelope: each face moves to its nearest grid plane (a
    face half way between two planes moves outward), a planar axis stays planar, any other axis keeps at least one cell."""
    near = [float(v) for v in box]
    for b in range(3):
        kmax = max(1, int(math.floor(env[b] / q + 1e-9)))
        t0, t1 = (near[b] - origin[b]) / q, (near[b + 3] - origin[b]) / q
        if abs(near[b + 3] - near[b]) <= _TOL:
            k = min(max(math.floor(t0 + 0.5), 0), kmax)
            near[b] = near[b + 3] = origin[b] + k * q
            continue
        k0 = min(max(math.floor(t0 + 0.5 - 1e-9), 0), kmax)
        k1 = min(max(math.ceil(t1 - 0.5 + 1e-9), 0), kmax)
        if k1 <= k0:
            k0, k1 = (k0, k0 + 1) if k0 < kmax else (k1 - 1, k1)
        near[b], near[b + 3] = origin[b] + k0 * q, origin[b] + k1 * q
    return near


def check_geometry(eff: dict) -> list[str]:
    """Geometry rules on the grid in eff["derived"]["grid"] (section 6.3b): one line per refusal (exit 2). Needs the grid:
    call it after the grid is known (explicit in the spec, or derive_grid after domain.build). Every face of every
    support and load box (minus the origin) must be a whole multiple of gen_mm, fea_mm and each planned check level
    (bc_lattice), so the generator, the solves and the independent check all fix and load the same patch; a traction
    plane must be an envelope face."""
    d = eff["derived"]
    grid = d.get("grid")
    if not grid:
        raise ValueError("check_geometry needs eff['derived']['grid']: derive it after domain.build")
    origin = [float(v) for v in grid["origin_mm"]]
    env = [float(v) for v in grid["envelope_mm"]]
    gen, fea, field_h = d["gen_mm"], d["fea_mm"], d["field_h_mm"]
    h1 = d["check_levels"][0]["h_mm"]
    out: list[str] = []
    try:
        q = grid_quantum([gen, fea, field_h, h1])
    except GoldenError as exc:
        return [exc.message]
    for a in range(3):
        L = env[a]
        for h, what in ((gen, "gen_mm"), (fea, "fea_mm"), (field_h, "the skin field spacing"), (h1, "the level-1 check")):
            k = L / h
            if abs(k - round(k)) > 1e-6:
                lo, hi = math.floor(L / q) * q, math.ceil(L / q) * q
                lo = lo if lo > 0 else hi
                out.append(f"domain.envelope_mm {AXES[a]} = {_fmt(L)} mm is not a whole number of {_fmt(h)} mm cells "
                           f"({what}): use {_fmt(lo)} or {_fmt(hi)}")
                break
    lo_env = origin
    hi_env = [origin[a] + env[a] for a in range(3)]

    def inside(bb: list[float]) -> bool:
        return all(bb[a] >= lo_env[a] - _TOL and bb[a + 3] <= hi_env[a] + _TOL for a in range(3))

    env_text = (f"[{', '.join(_fmt(v) for v in lo_env)}, {', '.join(_fmt(v) for v in hi_env)}]")
    items = ([("support", s, i) for i, s in enumerate(eff["supports"])] + [("load", ld, i) for i, ld in enumerate(eff["loads"])])
    for kind, item, i in items:
        if not inside(item["box"]):
            out.append(f"{_label(kind, item, i)} box {item['box']} is not inside the envelope {env_text}")
    for kind, key in (("keep-in", "keep_in"), ("keep-out", "keep_out")):
        for i, r in enumerate(eff["domain"][key]):
            if not inside(_region_bbox(r)):
                out.append(f"{_label(kind, r, i)} {_region_bbox(r)} is not inside the envelope {env_text}")
    try:
        qb, bc_hs = bc_lattice(eff)
    except GoldenError as exc:
        return out + [exc.message]
    shared = _and([f"{what} {_fmt(h)}" if what.endswith("_mm") else f"{what} {_fmt(h)} mm" for h, what in bc_hs])
    origin_text = ", ".join(_fmt(v) for v in origin)
    for kind, item, i in items:
        box = [float(v) for v in item["box"]]
        label = _label(kind, item, i)
        planar = [a for a in range(3) if abs(box[a + 3] - box[a]) <= _TOL]
        for a in planar:
            if not _on_lattice(box[a], origin[a], qb):
                lo, hi = _snap(box[a], origin[a], qb)
                out.append(f"{label}: plane {AXES[a]}={_fmt(box[a])} is not on the {_fmt(qb)} mm grid "
                           f"(origin {_fmt(origin[a])}): use {AXES[a]}={_fmt(lo)} or {AXES[a]}={_fmt(hi)}")
        bad = [b for b in range(3) if b not in planar
               and not (_on_lattice(box[b], origin[b], qb) and _on_lattice(box[b + 3], origin[b], qb))]
        traction = kind == "load" and bool(item.get("axis"))
        if bad:
            edges = ", ".join(f"{AXES[b]} {_fmt(box[b])}..{_fmt(box[b + 3])}" for b in bad)
            near = ", ".join(_fmt(v) for v in _near_box(box, origin, env, qb))
            out.append(f"{label}: {'traction' if traction else 'box'} edges {edges} are not on the {_fmt(qb)} mm grid "
                       f"that {shared} share (origin {origin_text}), so each resolution would "
                       f"{'load' if kind == 'load' else 'fix'} a different patch: use the box [{near}]")
        if traction and inside(box):
            a = AXES.index(item["axis"])
            if abs(box[a] - lo_env[a]) > _TOL and abs(box[a] - hi_env[a]) > _TOL:
                out.append(f"{label}: traction plane {AXES[a]}={_fmt(box[a])} is inside the envelope (its {AXES[a]} faces "
                           f"are {_fmt(lo_env[a])} and {_fmt(hi_env[a])}); a traction loads the element faces on both "
                           "sides of an interior plane, so it cannot load one side only: put the plane on an envelope "
                           "face (for a grid derived from a STEP, set domain.margin_mm to 0) or drop axis to make it a "
                           "nodal load")
    if float(d["patch_keep_in_mm"]) == 0.0:
        keep = [_region_bbox(r) for r in eff["domain"]["keep_in"]]
        for kind, item, i in items:
            box = item["box"]
            if not any(all(box[a] <= k[a + 3] + _TOL and box[a + 3] >= k[a] - _TOL for a in range(3)) for k in keep):
                out.append(f"{_label(kind, item, i)} is not on preserved material: add a keep-in around it "
                           "(or set domain.patch_keep_in_mm above 0)")
    n = math.prod(_cells(env, fea))
    if n > CHECK_CELL_LIMIT:
        out.append(f"{n:,} cells at fea_mm {_fmt(fea)} mm is over the v0 limit of {CHECK_CELL_LIMIT:,}: use a coarser --res")
    return out


def geometry_warnings(eff: dict, *, gpu_usable: bool) -> list[str]:
    """Warnings that do not refuse the spec: more than 200,000 cells at fea_mm on a host without a usable GPU."""
    grid = eff["derived"].get("grid")
    if not grid or gpu_usable:
        return []
    n = math.prod(_cells(grid["envelope_mm"], eff["derived"]["fea_mm"]))
    if n > CPU_CELL_WARNING:
        return [f"{n:,} cells at fea_mm {_fmt(eff['derived']['fea_mm'])} mm on a CPU-only host: each solve will be slow"]
    return []


# -------------------------------------------------------------------------------------------------------------- grids

def grid_quantum(values_mm: list[float]) -> float:
    """The smallest length that is a whole multiple of every value (exact LCM of the decimal values, each read through
    Fraction(str(h)).limit_denominator(1000)). GoldenError(EXIT_USAGE) when it exceeds 1000 mm."""
    q = Fraction(0)
    for v in values_mm:
        f = Fraction(str(float(v))).limit_denominator(1000)
        if f <= 0:
            raise GoldenError(EXIT_USAGE, f"grid resolution {v!r} mm must be positive")
        if q == 0:
            q = f
        else:
            num = q.numerator * f.numerator // math.gcd(q.numerator, f.numerator)
            q = Fraction(num, math.gcd(q.denominator, f.denominator))
    if q > 1000:
        listed = ", ".join(_fmt(float(v)) for v in values_mm)
        raise GoldenError(EXIT_USAGE, f"the resolutions {listed} mm share no grid below 1000 mm (common multiple "
                          f"{float(q):g} mm)", hint="use resolutions that divide each other")
    return float(q)


def derive_grid(bbox_mm: list[float], eff: dict) -> dict:
    """Grid for a STEP whose spec gives no origin/envelope: origin and envelope snapped outward to the grid quantum of
    gen_mm, fea_mm, the skin field spacing and the level-1 check, with domain.margin_mm around the STEP's bbox."""
    d = eff["derived"]
    q = grid_quantum([d["gen_mm"], d["fea_mm"], d["field_h_mm"], d["check_levels"][0]["h_mm"]])
    margin = float(eff["domain"]["margin_mm"])
    lo = [float(v) for v in bbox_mm[:3]]
    hi = [float(v) for v in bbox_mm[3:]]
    origin, env = [], []
    for a in range(3):
        o = math.floor((lo[a] - margin) / q + 1e-6) * q
        n = max(1, math.ceil((hi[a] + margin - o) / q - 1e-6))
        origin.append(round(o, 9))
        env.append(round(n * q, 9))
    return {"origin_mm": origin, "envelope_mm": env, "derived_from": "step-bbox"}


def param_values(eff: dict, params: dict) -> dict:
    """The generator parameters of one design: the fixed values overlaid with the design's variable values."""
    d = eff["derived"]
    out = {k: float(v) for k, v in d["fixed"].items()}
    missing = [k for k in d["variables"] if k not in params]
    if missing:
        raise ValueError(f"design parameters miss {', '.join(missing)}")
    for k in d["variables"]:
        out[k] = float(params[k])
    return out


def json_schema() -> dict:
    """The JSON Schema of aces-golden-spec/1 (keys by alias, so the version key is "schema")."""
    return GoldenSpec.model_json_schema(by_alias=True)

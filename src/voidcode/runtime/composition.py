"""Installed-package composition without a new execution or permission engine.

Packages ship one ``voidcode-components.json`` as distribution metadata or
owned package data. Pure host-side validators return a real typed config value
and its authoritative nonsecret snapshot; provider/tool implementations need
not import this runtime module. Factory ABI is ``factory(value, context)``.

Admission records actual implementation/resource and declared dependency-owner
bytes, verifies Python origins, then freezes one binding/plan. Pure declaration
and validator imports may occur before freezing; this is not an import sandbox.
Activation reads the owning repository before resolving executable adapters.
Construction is explicit and cached; consumers retain their existing registries.
Lifecycle callbacks own input validation, snapshot and restore behavior; the
caller supplies its existing durable record reader/writer, not a second ledger.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_serializer, field_validator

from ..security.json_values import json_wire_object, own_json_object

MANIFEST_NAME = "voidcode-components.json"
type ComponentSlot = Literal["provider", "tool", "agent", "context", "typed-input"]
type LifecycleScope = Literal["session", "run", "turn", "tool"]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def _strict_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("composition schema_version must be integer 1")
        return value


class DependencyDeclaration(_FrozenModel):
    distribution: str = Field(min_length=1)
    files: tuple[str, ...] = Field(min_length=1)


class ComponentDeclaration(_FrozenModel):
    slot: ComponentSlot
    name: str = Field(min_length=1)
    factory: str = Field(min_length=1)
    validate_config: str = Field(min_length=1)


class PhaseDeclaration(_FrozenModel):
    name: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    slot: ComponentSlot
    component: str = Field(min_length=1)
    callback: str = Field(min_length=1)
    validate_input: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)
    restore: str = Field(min_length=1)
    scope: LifecycleScope
    order: StrictInt
    version: str = Field(min_length=1)
    failure: Literal["raise", "continue"]
    replay: Literal["repeat", "once", "refuse"]


class PackageDeclaration(_FrozenModel):
    schema_version: Literal[1]
    files: tuple[str, ...] = Field(min_length=1)
    dependencies: tuple[DependencyDeclaration, ...] = ()
    components: tuple[ComponentDeclaration, ...] = ()
    phases: tuple[PhaseDeclaration, ...] = ()


class FileIdentity(_FrozenModel):
    path: str
    sha256: str


class ImplementationOwner(_FrozenModel):
    distribution: str
    version: str
    files: tuple[FileIdentity, ...]


class PackageIdentity(_FrozenModel):
    distribution: str
    version: str
    declaration_sha256: str
    files: tuple[FileIdentity, ...]
    dependencies: tuple[ImplementationOwner, ...]


class ComponentBinding(_FrozenModel):
    key: str
    distribution: str
    slot: ComponentSlot
    name: str
    configuration: Mapping[str, object]
    provenance: tuple[str, ...]

    @field_validator("configuration")
    @classmethod
    def _own_configuration(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        return own_json_object(value)

    @field_serializer("configuration")
    def _configuration_payload(self, value: Mapping[str, object]) -> dict[str, object]:
        return json_wire_object(value)


class CapabilityBinding(_FrozenModel):
    schema_version: Literal[1]
    binding_id: str
    packages: tuple[PackageIdentity, ...]
    components: tuple[ComponentBinding, ...]
    intent: Mapping[str, object]

    @field_validator("intent")
    @classmethod
    def _own_intent(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        return own_json_object(value)

    @field_serializer("intent")
    def _intent_payload(self, value: Mapping[str, object]) -> dict[str, object]:
        return json_wire_object(value)

    def verify(self) -> None:
        if self.binding_id != _digest(self.model_dump(mode="json", exclude={"binding_id"})):
            raise ValueError("capability binding hash does not match its frozen representation")


class PhaseBinding(_FrozenModel):
    key: str
    component_key: str
    distribution: str
    declaration: PhaseDeclaration


class ExecutionPlan(_FrozenModel):
    schema_version: Literal[1]
    plan_id: str
    binding_id: str
    components: tuple[str, ...]
    phases: tuple[PhaseBinding, ...]

    def verify(self) -> None:
        if self.plan_id != _digest(self.model_dump(mode="json", exclude={"plan_id"})):
            raise ValueError("execution plan hash does not match its frozen representation")


class SessionCompositionOwner(_FrozenModel):
    kind: Literal["session"]
    session_id: str


class TaskCompositionOwner(_FrozenModel):
    kind: Literal["task"]
    task_id: str


class CompositionRef(_FrozenModel):
    workspace: str
    owner: SessionCompositionOwner | TaskCompositionOwner = Field(discriminator="kind")
    binding_id: str
    plan_id: str

    @classmethod
    def from_frozen(
        cls,
        frozen: FrozenComposition,
        *,
        workspace: str,
        owner: SessionCompositionOwner | TaskCompositionOwner,
    ) -> CompositionRef:
        frozen.verify()
        return cls(workspace=workspace, owner=owner, binding_id=frozen.binding.binding_id, plan_id=frozen.plan.plan_id)


class FrozenComposition(_FrozenModel):
    binding: CapabilityBinding
    plan: ExecutionPlan

    def verify(self) -> None:
        self.binding.verify()
        self.plan.verify()
        if self.plan.binding_id != self.binding.binding_id:
            raise ValueError("execution plan references a different capability binding")
        if self.plan.components != tuple(component.key for component in self.binding.components):
            raise ValueError("execution plan component references do not match the binding")

    def to_payload(self) -> dict[str, object]:
        self.verify()
        return self.model_dump(mode="json")

    @classmethod
    def from_payload(cls, payload: object) -> FrozenComposition:
        frozen = cls.model_validate(payload)
        frozen.verify()
        return frozen

    def reference(
        self,
        *,
        workspace: str,
        owner: SessionCompositionOwner | TaskCompositionOwner,
    ) -> CompositionRef:
        return CompositionRef.from_frozen(self, workspace=workspace, owner=owner)


@dataclass(frozen=True, slots=True)
class ComponentSelection:
    distribution: str
    slot: ComponentSlot
    name: str
    configuration: object
    provenance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ValidatedConfig:
    """Package-owned pure value plus its authoritative nonsecret snapshot."""

    value: object
    snapshot: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot", own_json_object(self.snapshot))


@dataclass(frozen=True, slots=True)
class BuiltinDeclaration:
    """Existing builtin owners supply declarations and their actual source root."""

    declaration: PackageDeclaration
    root: Path
    distribution: str = "voidcode"


@dataclass(frozen=True, slots=True)
class _Package:
    declaration: PackageDeclaration
    identity: PackageIdentity
    implementation_files: Mapping[Path, str]
    modules: Mapping[str, Path]


@dataclass(frozen=True, slots=True)
class PhaseCompleted:
    key: str
    value: object


@dataclass(frozen=True, slots=True)
class PhaseFailed:
    key: str
    error: str


type PhaseOutcome = PhaseCompleted | PhaseFailed


class PhaseRecord(_FrozenModel):
    phase_key: str
    binding_id: str
    version: str
    scope: LifecycleScope
    scope_id: str
    failed: StrictBool
    payload: Mapping[str, object]

    @field_validator("payload")
    @classmethod
    def _own_payload(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        return own_json_object(value)

    @field_serializer("payload")
    def _payload_wire(self, value: Mapping[str, object]) -> dict[str, object]:
        return json_wire_object(value)


def _reference_module(reference: str) -> str:
    module, separator, attribute = reference.partition(":")
    if not separator or not module or not attribute or any(not part.isidentifier() for part in (*module.split("."), *attribute.split("."))):
        raise ValueError(f"invalid component reference: {reference}")
    return module


def _load(package: _Package, reference: str) -> Callable[..., object]:
    # Resolve actual Python origins, not just distribution RECORD claims.
    for name, expected in package.modules.items():
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None or Path(spec.origin).resolve() != expected:
            raise ValueError(f"component module is shadowed or belongs to a different implementation owner: {name}")
        loaded = sys.modules.get(name)
        if loaded is not None and Path(getattr(loaded, "__file__", "")).resolve() != expected:
            raise ValueError(f"cached component module belongs to a different implementation owner: {name}")
    module = importlib.import_module(_reference_module(reference))
    value: object = module
    for attribute in reference.partition(":")[2].split("."):
        value = getattr(value, attribute)
    if not callable(value):
        raise ValueError(f"component reference is not callable: {reference}")
    try:
        implementation = Path(inspect.getfile(value)).resolve()
    except TypeError as error:
        raise ValueError(f"component callable has no declared implementation owner: {reference}") from error
    expected_digest = package.implementation_files.get(implementation)
    if expected_digest is None:
        raise ValueError(f"component callback implementation is outside its declared owners: {reference}")
    with implementation.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != expected_digest:
            raise ValueError(f"component implementation changed after admission: {reference}")
    return value


def _implementation_locations(root: Path, files: tuple[FileIdentity, ...]) -> tuple[dict[Path, str], dict[str, Path]]:
    identities: dict[Path, str] = {}
    modules: dict[str, Path] = {}
    for identity in files:
        path = (root / identity.path).resolve()
        identities[path] = identity.sha256
        if identity.path.endswith(".py"):
            module = identity.path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[:-9]
            modules[module] = path
    return identities, modules


def _manifest(distribution: metadata.Distribution) -> str | None:
    raw = distribution.read_text(MANIFEST_NAME)
    if raw is not None:
        return raw
    files = tuple(file for file in distribution.files or () if PurePosixPath(str(file)).name == MANIFEST_NAME)
    if len(files) > 1:
        raise ValueError("installed package contains ambiguous component declarations")
    if not files:
        return None
    root = Path(str(distribution.locate_file(""))).resolve()
    path = Path(str(distribution.locate_file(files[0]))).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("component declaration is outside its installed owner")
    return path.read_text(encoding="utf-8")


def _file_identities(root: Path, names: tuple[str, ...]) -> tuple[FileIdentity, ...]:
    result: list[FileIdentity] = []
    if len(set(names)) != len(names):
        raise ValueError("implementation owner contains duplicate file declarations")
    root = root.resolve()
    for name in sorted(names):
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"invalid owned implementation/resource path: {name}")
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"owned implementation/resource is unavailable: {name}")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        result.append(FileIdentity(path=name, sha256=digest))
    return tuple(result)


def _check_declaration(declaration: PackageDeclaration) -> None:
    components = {(component.slot, component.name) for component in declaration.components}
    if len(components) != len(declaration.components) or len({phase.name for phase in declaration.phases}) != len(declaration.phases):
        raise ValueError("package contains duplicate component or lifecycle declarations")
    references = [reference for component in declaration.components for reference in (component.factory, component.validate_config)]
    for phase in declaration.phases:
        if (phase.slot, phase.component) not in components:
            raise ValueError("lifecycle phase references an undeclared component")
        references.extend((phase.callback, phase.validate_input, phase.snapshot, phase.restore))
    files = frozenset(declaration.files)
    for reference in references:
        module_path = _reference_module(reference).replace(".", "/")
        if f"{module_path}.py" not in files and f"{module_path}/__init__.py" not in files:
            raise ValueError(f"component code is not declared as an owned implementation: {reference}")


class CompositionOwner:
    """Static admission and one session-frozen plan; not a permission authority."""

    def __init__(self, *, builtins: tuple[BuiltinDeclaration, ...] = ()) -> None:
        self._builtins = {_name(item.distribution): item for item in builtins}
        self._packages: dict[str, _Package] = {}
        self._values: dict[tuple[str, str], object] = {}

    def discover(self) -> tuple[str, ...]:
        names = set(self._builtins)
        for distribution in metadata.distributions():
            if _manifest(distribution) is not None:
                names.add(_name(distribution.metadata["Name"]))
        return tuple(sorted(names))

    def _package(self, name: str, *, refresh: bool = False) -> _Package:
        name = _name(name)
        if not refresh and name in self._packages:
            return self._packages[name]
        distribution = metadata.distribution(name)
        builtin = self._builtins.get(name)
        if builtin is None:
            raw = _manifest(distribution)
            if raw is None:
                raise ValueError(f"installed package has no component declaration: {name}")
            declaration = PackageDeclaration.model_validate_json(raw)
            root = Path(str(distribution.locate_file("")))
            recorded_files = {str(file) for file in distribution.files or ()}
            if not set(declaration.files).issubset(recorded_files):
                raise ValueError("package claims implementation/resources outside its installed distribution")
        else:
            declaration, root = builtin.declaration, builtin.root
        _check_declaration(declaration)
        dependencies: list[ImplementationOwner] = []
        owned_files = _file_identities(root, declaration.files)
        implementation_files, modules = _implementation_locations(root, owned_files)
        for dependency in declaration.dependencies:
            owner = metadata.distribution(dependency.distribution)
            builtin_owner = self._builtins.get(_name(dependency.distribution))
            if builtin_owner is None:
                if not set(dependency.files).issubset({str(file) for file in owner.files or ()}):
                    raise ValueError("dependency implementation is outside its installed owner")
                dependency_root = Path(str(owner.locate_file("")))
            else:
                if not set(dependency.files).issubset(builtin_owner.declaration.files):
                    raise ValueError("dependency implementation is outside its authoritative builtin owner")
                dependency_root = builtin_owner.root
            files = _file_identities(dependency_root, dependency.files)
            identities, dependency_modules = _implementation_locations(dependency_root, files)
            if any(name in modules and modules[name] != path for name, path in dependency_modules.items()):
                raise ValueError("declared implementation owners conflict on a module")
            implementation_files.update(identities)
            modules.update(dependency_modules)
            dependencies.append(ImplementationOwner(distribution=_name(owner.metadata["Name"]), version=owner.version, files=files))
        if len({owner.distribution for owner in dependencies}) != len(dependencies):
            raise ValueError("package contains duplicate dependency implementation owners")
        dependencies.sort(key=lambda item: item.distribution)
        identity = PackageIdentity(
            distribution=name,
            version=distribution.version,
            declaration_sha256=_digest(declaration.model_dump(mode="json")),
            files=owned_files,
            dependencies=tuple(dependencies),
        )
        package = _Package(declaration, identity, MappingProxyType(implementation_files), MappingProxyType(modules))
        self._packages[name] = package
        return package

    def prepare(self, selections: Sequence[ComponentSelection], *, intent: Mapping[str, object]) -> FrozenComposition:
        packages: dict[str, _Package] = {}
        bindings: list[ComponentBinding] = []
        values: dict[str, object] = {}
        for selection in selections:
            name = _name(selection.distribution)
            package = packages.get(name)
            if package is None:
                package = self._package(name, refresh=True)
                packages[name] = package
            declaration = next((item for item in package.declaration.components if item.slot == selection.slot and item.name == selection.name), None)
            if declaration is None:
                raise ValueError("selected component is not declared by its installed package")
            config = own_json_object(selection.configuration) if isinstance(selection.configuration, Mapping) else selection.configuration
            validated = _load(package, declaration.validate_config)(config)
            if not isinstance(validated, ValidatedConfig):
                raise ValueError("pure config validator must return ValidatedConfig")
            key = f"{name}:{selection.slot}:{selection.name}"
            if key in values:
                raise ValueError("component selected more than once")
            values[key] = validated.value
            bindings.append(
                ComponentBinding(
                    key=key,
                    distribution=name,
                    slot=selection.slot,
                    name=selection.name,
                    configuration=validated.snapshot,
                    provenance=selection.provenance,
                )
            )
        payload = {
            "schema_version": 1,
            "packages": [package.identity.model_dump(mode="json") for _, package in sorted(packages.items())],
            "components": [item.model_dump(mode="json") for item in bindings],
            "intent": json_wire_object(own_json_object(intent)),
        }
        binding = CapabilityBinding(binding_id=_digest(payload), **payload)
        phases: list[PhaseBinding] = []
        selected = frozenset(item.key for item in bindings)
        for name, package in sorted(packages.items()):
            for phase in package.declaration.phases:
                key = f"{name}:{phase.slot}:{phase.component}"
                if key in selected:
                    phases.append(PhaseBinding(key=f"{name}:{phase.name}", component_key=key, distribution=name, declaration=phase))
        phases.sort(key=lambda phase: phase.declaration.order)
        plan_payload = {
            "schema_version": 1,
            "binding_id": binding.binding_id,
            "components": tuple(item.key for item in bindings),
            "phases": tuple(phases),
        }
        plan = ExecutionPlan(plan_id=_digest({**plan_payload, "phases": [phase.model_dump(mode="json") for phase in phases]}), **plan_payload)
        for key, value in values.items():
            self._values[(binding.binding_id, key)] = value
        return FrozenComposition(binding=binding, plan=plan)

    def validated_value(self, frozen: FrozenComposition, key: str) -> object:
        """Return a pure validated config value before its post-persistence factory gate."""
        frozen.verify()
        if key not in {component.key for component in frozen.binding.components}:
            raise ValueError("component is outside the frozen plan")
        try:
            return self._values[(frozen.binding.binding_id, key)]
        except KeyError as error:
            raise ValueError("component config was not prepared by this owner") from error

    def refresh(self, frozen: FrozenComposition) -> None:
        frozen.verify()
        for identity in frozen.binding.packages:
            current = self._package(identity.distribution, refresh=True)
            if current.identity != identity:
                raise ValueError(f"frozen implementation/resource identity changed: {identity.distribution}")
        recovered = self.prepare(
            tuple(
                ComponentSelection(item.distribution, item.slot, item.name, item.configuration, item.provenance) for item in frozen.binding.components
            ),
            intent=frozen.binding.intent,
        )
        if recovered != frozen:
            raise ValueError("frozen package config or lifecycle plan no longer validates identically")

    def activate(self, frozen: FrozenComposition, *, read_persisted: Callable[[], FrozenComposition | None]) -> ActiveComposition:
        """The reader must load the actual owning repository, not echo this input."""
        recorded = read_persisted()
        if recorded is None or recorded != frozen:
            raise ValueError("single validated execution plan is not durably persisted")
        self.refresh(recorded)
        values = {component.key: self._values[(recorded.binding.binding_id, component.key)] for component in recorded.binding.components}
        factories: dict[str, Callable[..., object]] = {}
        phase_functions: dict[str, tuple[Callable[..., object], ...]] = {}
        for binding in recorded.binding.components:
            package = self._packages[binding.distribution]
            declaration = next(item for item in package.declaration.components if item.slot == binding.slot and item.name == binding.name)
            factories[binding.key] = _load(package, declaration.factory)
        for phase in recorded.plan.phases:
            declaration = phase.declaration
            package = self._packages[phase.distribution]
            phase_functions[phase.key] = tuple(
                _load(package, reference)
                for reference in (declaration.validate_input, declaration.callback, declaration.snapshot, declaration.restore)
            )
        return ActiveComposition(recorded, values=values, factories=factories, phase_functions=phase_functions)


class ActiveComposition:
    """Post-persistence lazy construction; consumers retain their real registries."""

    def __init__(
        self,
        frozen: FrozenComposition,
        *,
        values: Mapping[str, object],
        factories: Mapping[str, Callable[..., object]],
        phase_functions: Mapping[str, tuple[Callable[..., object], ...]],
    ) -> None:
        self.frozen = frozen
        self._instances: dict[str, object] = {}
        self._values = values
        self._factories = factories
        self._phase_functions = phase_functions

    def admitted_value(self, key: str) -> object:
        if key not in self._values:
            raise ValueError("component is outside the frozen plan")
        return self._values[key]

    def construct(self, key: str, *, context: object) -> object:
        if key not in self._factories:
            raise ValueError("component is outside the frozen plan")
        if key not in self._instances:
            self._instances[key] = self._factories[key](self._values[key], context)
        return self._instances[key]

    def run_phase(
        self,
        name: str,
        value: object,
        *,
        scope: LifecycleScope,
        scope_id: str,
        context: object,
        read_record: Callable[[str], PhaseRecord | None],
        write_record: Callable[[PhaseRecord], None],
        recovering: bool = False,
    ) -> tuple[PhaseOutcome, ...]:
        """Restore once-only outcomes, but never turn a recorded fatal failure into continuation."""
        if not scope_id:
            raise ValueError("lifecycle phase requires its actual scope identity")
        outcomes: list[PhaseOutcome] = []
        for phase in self.frozen.plan.phases:
            declaration = phase.declaration
            if declaration.trigger != name or declaration.scope != scope:
                continue
            record_key = f"{phase.key}:{scope}:{scope_id}"
            record = read_record(record_key)
            validate_input, callback, snapshot, restore = self._phase_functions[phase.key]
            if record is not None:
                if (record.phase_key, record.binding_id, record.version, record.scope, record.scope_id) != (
                    phase.key,
                    self.frozen.binding.binding_id,
                    declaration.version,
                    scope,
                    scope_id,
                ):
                    raise ValueError("recorded lifecycle scope/version does not match the frozen plan")
            if recovering and record is None and declaration.replay == "refuse":
                raise ValueError("package lifecycle phase refuses unrecorded recovery execution")
            typed_input = validate_input(value)
            instance = self.construct(phase.component_key, context=context)
            if record is not None and declaration.replay != "repeat":
                restored = restore(record.payload, instance, context)
                if record.failed:
                    error = str(restored)
                    if declaration.failure == "raise":
                        raise RuntimeError(error)
                    outcomes.append(PhaseFailed(record_key, error))
                else:
                    outcomes.append(PhaseCompleted(record_key, restored))
                continue
            try:
                result = callback(typed_input, instance, context)
            except Exception as error:
                outcome: PhaseOutcome = PhaseFailed(record_key, str(error))
                payload = snapshot(outcome)
                write_record(
                    PhaseRecord.model_validate(
                        {
                            "phase_key": phase.key,
                            "binding_id": self.frozen.binding.binding_id,
                            "version": declaration.version,
                            "scope": scope,
                            "scope_id": scope_id,
                            "failed": True,
                            "payload": payload,
                        }
                    )
                )
                if declaration.failure == "raise":
                    raise
            else:
                outcome = PhaseCompleted(record_key, result)
                payload = snapshot(outcome)
                write_record(
                    PhaseRecord.model_validate(
                        {
                            "phase_key": phase.key,
                            "binding_id": self.frozen.binding.binding_id,
                            "version": declaration.version,
                            "scope": scope,
                            "scope_id": scope_id,
                            "failed": False,
                            "payload": payload,
                        }
                    )
                )
            outcomes.append(outcome)
        return tuple(outcomes)

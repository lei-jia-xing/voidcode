"""Typed frames exchanged between TUI workers and the UI thread."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from voidcode.sdk.client import CommandCatalog, CommandResult, EventFrame, HeadFrame, HealthStatus, SessionRow
from voidcode.sdk.commands import ConfirmationRequest
from voidcode.sdk.services import ArtifactPage
from voidcode.sdk.views import PluginViewDescriptor, ViewsSnapshot

from .artifact_viewer import InteractiveArtifactViewer
from .formatting import NoticeTone
from .gates import ApprovalGate
from .term import TerminalKittyReply
from .views import InteractiveChoices, InteractiveConfirmation, InteractiveForm, InteractiveMarkdown


class AbortToken:
    """Single-flight invalidation for one async producer round."""

    __slots__ = ("aborted",)

    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        """Supersede this round; frames carrying the token must be dropped."""
        self.aborted = True


@dataclass(frozen=True, slots=True, kw_only=True)
class TuiEvent:
    """Base event with the optional staleness guards applied at the drain boundary."""

    session_id: str | None = None
    generation: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WakeEvent(TuiEvent):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class InputEvent(TuiEvent):
    chunk: bytes
    arrived_at: float


@dataclass(frozen=True, slots=True, kw_only=True)
class InputEofEvent(TuiEvent):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class KittyReplyEvent(TuiEvent):
    reply: TerminalKittyReply


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamHeadEvent(TuiEvent):
    frame: HeadFrame


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamEventEvent(TuiEvent):
    frame: EventFrame


@dataclass(frozen=True, slots=True, kw_only=True)
class ServerRestartEvent(TuiEvent):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamErrorEvent(TuiEvent):
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthEvent(TuiEvent):
    health: HealthStatus
    token: AbortToken


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconciledEvent(TuiEvent):
    catalog: CommandCatalog | None
    session_status: str
    failures: tuple[str, ...]
    token: AbortToken


@dataclass(frozen=True, slots=True, kw_only=True)
class NoticeEvent(TuiEvent):
    text: str
    tone: NoticeTone = "info"


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandDoneEvent(TuiEvent):
    name: str
    status: str
    result: CommandResult | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandInputReadyEvent(TuiEvent):
    token: AbortToken
    revision_id: str
    view: PluginViewDescriptor | None
    error: str | None
    refresh_after_ms: int | None
    action: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveInputReadyEvent(TuiEvent):
    token: AbortToken
    revision_id: str
    action: str
    arguments: Mapping[str, object]
    view: PluginViewDescriptor | None
    error: str | None
    refresh_after_ms: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandPresentationEvent(TuiEvent):
    token: AbortToken
    view: PluginViewDescriptor
    revision_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GateAcceptedEvent(TuiEvent):
    request_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionDoneEvent(TuiEvent):
    request_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class InvokeAcceptedEvent(TuiEvent):
    request_id: str
    action: str
    revision_id: str
    overlay: InteractiveChoices | InteractiveConfirmation | InteractiveForm | InteractiveMarkdown | InteractiveArtifactViewer | None


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStatusEvent(TuiEvent):
    status: str
    error: str | None = None
    run_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelDoneEvent(TuiEvent):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class FollowupQueuedEvent(TuiEvent):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class FollowupFailedEvent(TuiEvent):
    prompt: str
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RunRejectedEvent(TuiEvent):
    prompt: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RunWaitingEvent(TuiEvent):
    gate: ApprovalGate


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfirmationEvent(TuiEvent):
    confirmation: ConfirmationRequest
    revision_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class FormLoadEvent(TuiEvent):
    form: InteractiveForm
    data: Mapping[str, object] | None
    error: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionsLoadedEvent(TuiEvent):
    sessions: tuple[SessionRow, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionsFailedEvent(TuiEvent):
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionCreatedEvent(TuiEvent):
    new_session_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionCreateFailedEvent(TuiEvent):
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewsLoadedEvent(TuiEvent):
    snapshot: ViewsSnapshot
    name_filter: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewsFailedEvent(TuiEvent):
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelSwitchDoneEvent(TuiEvent):
    ok: bool
    revision: str = ""
    stage: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactEvent(TuiEvent):
    """Base for one artifact-viewer reply; each operation carries only its fields."""

    viewer: str
    request_id: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactPageEvent(ArtifactEvent):
    page: ArtifactPage


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactErrorEvent(ArtifactEvent):
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactOpenEvent(ArtifactEvent):
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactImageProgressEvent(ArtifactEvent):
    sha256: str
    received: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactImageEvent(ArtifactEvent):
    sha256: str
    data: bytes


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactImageErrorEvent(ArtifactEvent):
    sha256: str
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactTransmittedEvent(ArtifactEvent):
    sha256: str


type TuiFrame = (
    WakeEvent
    | InputEvent
    | InputEofEvent
    | KittyReplyEvent
    | StreamHeadEvent
    | StreamEventEvent
    | ServerRestartEvent
    | StreamErrorEvent
    | HealthEvent
    | ReconciledEvent
    | NoticeEvent
    | CommandDoneEvent
    | CommandInputReadyEvent
    | LiveInputReadyEvent
    | CommandPresentationEvent
    | GateAcceptedEvent
    | DecisionDoneEvent
    | InvokeAcceptedEvent
    | RunStatusEvent
    | CancelDoneEvent
    | FollowupQueuedEvent
    | FollowupFailedEvent
    | RunRejectedEvent
    | RunWaitingEvent
    | ConfirmationEvent
    | FormLoadEvent
    | SessionsLoadedEvent
    | SessionsFailedEvent
    | SessionCreatedEvent
    | SessionCreateFailedEvent
    | ViewsLoadedEvent
    | ViewsFailedEvent
    | ModelSwitchDoneEvent
    | ArtifactPageEvent
    | ArtifactErrorEvent
    | ArtifactOpenEvent
    | ArtifactImageProgressEvent
    | ArtifactImageEvent
    | ArtifactImageErrorEvent
    | ArtifactTransmittedEvent
)

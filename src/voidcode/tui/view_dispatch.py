"""Finite plugin-view dispatch over the typed view data union."""

from __future__ import annotations

from voidcode.sdk.views import (
    ChoicesViewData,
    ConfirmationViewData,
    DiffViewData,
    FieldsViewData,
    FormViewData,
    JsonViewData,
    MarkdownViewData,
    MediaViewData,
    NoticeViewData,
    PluginViewDescriptor,
    StatusViewData,
    TableViewData,
    parse_view_data,
)

from .styles import Styles
from .view_forms import InteractiveForm
from .view_overlays import InteractiveChoices, InteractiveConfirmation
from .view_renderers import (
    render_diff,
    render_fields,
    render_json,
    render_markdown,
    render_media,
    render_notice,
    render_status,
    render_table,
)


class UnsupportedViewError(ValueError):
    """Raised when a plugin attempts to render an unrecognised view type."""


def render_plugin_view(descriptor: PluginViewDescriptor, width: int, styles: Styles | None = None) -> list[str]:
    """Render any of the 11 finite view kinds into ANSI rows.

    Raises UnsupportedViewError if the view_type is outside the 11 finite kinds.
    """
    data = parse_view_data(descriptor.view_type, descriptor.data)

    if isinstance(data, NoticeViewData):
        return render_notice(data, width, styles)
    if isinstance(data, MarkdownViewData):
        return render_markdown(data, width, styles)
    if isinstance(data, FieldsViewData):
        return render_fields(data, width, styles)
    if isinstance(data, DiffViewData):
        return render_diff(data, width, styles)
    if isinstance(data, StatusViewData):
        return render_status(data, width, styles)
    if isinstance(data, TableViewData):
        return render_table(data, width, styles)
    if isinstance(data, JsonViewData):
        return render_json(data, width, styles)
    if isinstance(data, MediaViewData):
        return render_media(data, width, styles)
    if isinstance(data, ChoicesViewData):
        return InteractiveChoices(data).render_rows(width, styles)
    if isinstance(data, ConfirmationViewData):
        return InteractiveConfirmation(data).render_rows(width, styles)
    if isinstance(data, FormViewData):
        return InteractiveForm(data).render_rows(width, styles)

    raise UnsupportedViewError(f"unsupported view type: {descriptor.view_type}")

"""File/photo/folder composer and durable-history previews for StudyCopilot."""
import asyncio

import flet as ft

from pages.context import (ctx, FS_XS, FS_SM, FS_MD, FS_XL, SP_XS, SP_SM,
                           SP_XL, R_MD, text_primary, text_secondary,
                           surface_hi, border_color)
from pages.components import open_dialog, close_dialog
from paperpilot.agent_attachments import (AttachmentError, IMAGE_EXTENSIONS,
    SUPPORTED_EXTENSIONS, prepare_selection, read_asset)


def _update(control):
    try:
        control.update()
    except RuntimeError:
        pass


def show_preview(name, scope, excerpt, warnings, images):
    content = [ft.Text(name, size=FS_MD, color=text_primary(), selectable=True),
               ft.Text(scope, size=FS_SM, color=text_secondary())]
    if warnings:
        content.append(ft.Text("；".join(warnings), size=FS_SM, color=text_primary()))
    if images:
        content.extend(ft.Image(src=data, height=SP_XL * 12, fit=ft.BoxFit.CONTAIN) for data in images)
    if excerpt:
        content.append(ft.Text(excerpt, size=FS_MD, color=text_primary(), selectable=True))
    dialog = ft.AlertDialog(title=ft.Text("附件预览"),
        content=ft.Container(width=SP_XL * 20, height=SP_XL * 17,
            content=ft.Column(content, scroll=ft.ScrollMode.AUTO, spacing=SP_SM)),
        actions=[ft.TextButton("关闭", on_click=lambda e: close_dialog(ctx.page, dialog))])
    open_dialog(ctx.page, dialog)


def history_attachments(directory, refs):
    def preview(ref):
        try:
            images = [read_asset(directory, image) for image in ref.get("images", [])]
            # Detect missing original assets even for textual previews.
            read_asset(directory, ref)
            show_preview(ref["name"], ref["scope"], ref.get("excerpt", ""), ref.get("warnings", []), images)
        except (ValueError, OSError) as exc:
            dialog = ft.AlertDialog(title=ft.Text("附件无法打开"), content=ft.Text(str(exc)),
                actions=[ft.TextButton("关闭", on_click=lambda e: close_dialog(ctx.page, dialog))])
            open_dialog(ctx.page, dialog)
    return ft.Column([ft.TextButton(content=ft.Row([
        ft.Icon(ft.Icons.IMAGE_OUTLINED if ref.get("images") else ft.Icons.ATTACH_FILE, size=FS_XL),
        ft.Text(ref["name"], size=FS_SM, expand=True, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
    ], spacing=SP_XS), tooltip="查看已保存的附件", on_click=lambda e, ref=ref: preview(ref)) for ref in refs],
        spacing=SP_XS, tight=True)


class AttachmentComposer:
    """Drafts are isolated per chat; picker results belong to their original chat."""
    def __init__(self, identity, on_change, is_busy):
        self.identity, self.on_change, self.is_busy = identity, on_change, is_busy
        self.drafts, self.notes = {}, {}
        self.loading = False
        self.picker = ft.FilePicker()
        ctx.page.services.append(self.picker)
        self.list = ft.Column(spacing=SP_XS, scroll=ft.ScrollMode.AUTO)
        self.note = ft.Text("", size=FS_XS, color=text_secondary())
        self.list_host = ft.Container(content=self.list, height=SP_XL * 4)
        self.panel = ft.Column([self.note, self.list_host],
                               spacing=SP_XS, visible=False)
        self.host = ft.Container(content=self.panel, key=ft.ValueKey("agent-attachment-host"),
            padding=ft.padding.Padding(left=SP_SM, top=SP_XS, right=SP_SM, bottom=SP_XS))
        self.button = ft.PopupMenuButton(icon=ft.Icons.ADD, tooltip="添加文件、照片或文件夹",
            key=ft.ValueKey("agent-attachment-add"), items=[
                ft.PopupMenuItem(content=ft.Text("添加文件"), on_click=lambda e: ctx.page.run_task(self.pick, "files")),
                ft.PopupMenuItem(content=ft.Text("添加照片"), on_click=lambda e: ctx.page.run_task(self.pick, "photos")),
                ft.PopupMenuItem(content=ft.Text("添加文件夹"), on_click=lambda e: ctx.page.run_task(self.pick, "folder")),
            ])

    @property
    def pending(self):
        return self.drafts.get(self.identity(), [])

    def clear(self, identity):
        self.drafts.pop(identity, None)
        self.notes.pop(identity, None)
        self.refresh()

    def remove(self, identity, item_id):
        self.drafts[identity] = [item for item in self.drafts.get(identity, []) if item.id != item_id]
        self.notes.pop(identity, None)
        self.refresh()
        self.on_change()

    def refresh(self):
        identity = self.identity()
        items = self.pending
        self.list_host.visible = bool(items)
        self.button.disabled = self.loading or self.is_busy()
        self.panel.visible = bool(items or self.notes.get(identity) or self.loading)
        self.note.color = text_secondary()
        self.note.value = ("正在读取所选资料…" if self.loading else
            f"待发送 {len(items)} 个附件 · {sum(len(i.data) for i in items) / 1024:.0f} KiB"
            + ("\n" + self.notes[identity] if self.notes.get(identity) else ""))
        rows = []
        for item in items:
            icon = (ft.Image(src=item.images[0].data, width=SP_XL + SP_SM, height=SP_XL + SP_SM, fit=ft.BoxFit.CONTAIN)
                    if item.images else ft.Icon(ft.Icons.ATTACH_FILE, size=FS_XL))
            preview = ft.TextButton(content=ft.Row([icon, ft.Text(item.name, size=FS_SM,
                color=text_primary(), expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)], spacing=SP_SM),
                expand=True, tooltip=item.scope + ("；" + "；".join(item.warnings) if item.warnings else ""),
                on_click=lambda e, item=item: show_preview(item.name, item.scope, item.excerpt, item.warnings, [i.data for i in item.images]))
            remove = ft.IconButton(icon=ft.Icons.CLOSE, icon_size=FS_XL, tooltip="移除 " + item.name,
                disabled=self.loading, on_click=lambda e, item=item, identity=identity: self.remove(identity, item.id))
            rows.append(ft.Container(content=ft.Row([preview, remove], spacing=SP_XS),
                bgcolor=surface_hi(), border_radius=R_MD, border=ft.Border.all(1, border_color())))
        self.list.controls = rows
        for control in (self.host, self.button):
            _update(control)

    async def pick(self, mode):
        if self.loading or self.is_busy():
            return
        identity = self.identity()
        self.loading = True
        self.refresh()
        self.on_change()
        try:
            if mode == "folder":
                directory = await self.picker.get_directory_path(dialog_title="选择提供给 Agent 的资料文件夹")
                if not directory:
                    return
                kwargs = dict(directory=directory)
            else:
                extensions = IMAGE_EXTENSIONS if mode == "photos" else SUPPORTED_EXTENSIONS
                files = await self.picker.pick_files(dialog_title="选择照片" if mode == "photos" else "选择资料文件",
                    file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=sorted(e[1:] for e in extensions), allow_multiple=True)
                if not files:
                    return
                if any(not f.path for f in files):
                    raise AttachmentError("当前入口需要桌面本地文件路径。")
                kwargs = dict(paths=[f.path for f in files])
            items, note = await asyncio.to_thread(prepare_selection, **kwargs, existing=self.drafts.get(identity, []))
            self.drafts[identity], self.notes[identity] = items, note
        except (AttachmentError, OSError) as exc:
            self.notes[identity] = str(exc)
        finally:
            self.loading = False
            self.refresh()
            self.on_change()

from __future__ import annotations
from dataclasses import dataclass, replace
from pathlib import Path
import os
import uuid


@dataclass(frozen=True)
class DocumentRecord:
    key: str
    project: str
    project_name: str
    name: str
    file_stem: str
    content: str = ""

    def model_copy(self, update=None):
        return replace(self, **(update or {}))


class DocumentService:
    def __init__(self, settings, service=None):
        self.settings = settings
        self.service = service
        self._studio_store = None

    def _document_path(self, project):
        if self.service is None:
            return self.settings.docs_dir / str(project) / "document.md"
        return self.service.document_path(str(project))

    def write_markdown(self, document):
        path = self._document_path(document.project)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(document.content or "", encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path


def render_requirement_body(requirement):
    return requirement.content or ""

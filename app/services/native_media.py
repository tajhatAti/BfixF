"""Preserve Telegram media presentation, not an octet-stream/.bin document."""
from __future__ import annotations

import copy
import mimetypes

from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo


def is_video(message) -> bool:
    doc = getattr(message, 'document', None)
    mime = str(getattr(doc, 'mime_type', '') or getattr(getattr(message, 'file', None), 'mime_type', ''))
    return bool(getattr(message, 'video', None) or mime.startswith('video/') or any(
        isinstance(a, DocumentAttributeVideo) for a in getattr(doc, 'attributes', []) or []))


def extension(message, kind: str = '') -> str:
    if getattr(message, 'photo', None) or kind == 'Photo':
        return '.jpg'
    mime = str(getattr(getattr(message, 'document', None), 'mime_type', '') or
               getattr(getattr(message, 'file', None), 'mime_type', '') or '')
    known = {'video/mp4': '.mp4', 'video/webm': '.webm', 'video/x-matroska': '.mkv',
             'video/quicktime': '.mov', 'video/x-msvideo': '.avi'}
    if kind == 'Video' or is_video(message):
        return known.get(mime, '.mp4')
    ext = mimetypes.guess_extension(mime) or '.dat'
    return ext if len(ext) <= 10 else '.dat'


def upload_options(message, kind: str = '') -> dict:
    video = kind == 'Video' or is_video(message)
    if getattr(message, 'photo', None) or kind == 'Photo':
        return {'force_document': False}
    doc = getattr(message, 'document', None)
    attrs = [copy.copy(a) for a in (getattr(doc, 'attributes', None) or [])
             if not isinstance(a, DocumentAttributeFilename)]
    ext = extension(message, kind)
    # Do not propagate a source filename such as file.bin into the video card.
    attrs.append(DocumentAttributeFilename(f'{"video" if video else "media"}-{message.id}{ext}'))
    mime = getattr(doc, 'mime_type', None) or mimetypes.guess_type('media' + ext)[0]
    if video:
        if not str(mime or '').startswith('video/'):
            mime = 'video/mp4'
        existing = next((a for a in attrs if isinstance(a, DocumentAttributeVideo)), None)
        if existing is None:
            source = getattr(message, 'video', None)
            existing = DocumentAttributeVideo(
                duration=getattr(source, 'duration', 0) or 0,
                w=getattr(source, 'w', 0) or 0, h=getattr(source, 'h', 0) or 0,
                supports_streaming=True)
            attrs.append(existing)
        existing.supports_streaming = True
    return {'attributes': attrs, 'mime_type': mime or 'application/octet-stream',
            'force_document': not video, 'supports_streaming': video}

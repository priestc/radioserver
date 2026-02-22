from __future__ import annotations

from pathlib import Path

from django.http import FileResponse, Http404
from django.contrib.admin.views.decorators import staff_member_required

from library.models import Album


@staff_member_required
def cover_art(request, album_id):
    try:
        album = Album.objects.get(pk=album_id)
    except Album.DoesNotExist:
        raise Http404

    track = album.tracks.first()
    if not track:
        raise Http404

    album_dir = Path(track.file_path).parent
    for name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png"):
        cover = album_dir / name
        if cover.exists():
            return FileResponse(open(cover, "rb"))

    raise Http404

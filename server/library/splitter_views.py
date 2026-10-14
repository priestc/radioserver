"""Staff-only pages and JSON endpoints for cutting SplitJobs into tracks."""
from __future__ import annotations

import json
from pathlib import Path

from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from library.models import SplitJob
from library.splitter import (
    SplitError, compute_waveform, cut_and_import, ensure_playback_proxy, validate_tracks,
)
from library.views import ranged_file_response


def _job_summary(job: SplitJob) -> dict:
    return {
        "id": job.id,
        "album_title": job.album_title,
        "status": job.status,
        "track_count": len(job.tracks),
        "review_notes": job.review_notes,
        "imported_at": job.imported_at.isoformat() if job.imported_at else None,
        "updated_at": job.updated_at.isoformat(),
    }


@staff_member_required
@require_GET
def splitter_index(request):
    jobs = SplitJob.objects.all()
    return render(request, "library/splitter_index.html", {
        "jobs": [_job_summary(j) for j in jobs],
    })


@staff_member_required
@require_GET
def splitter_editor(request, job_id):
    job = get_object_or_404(SplitJob, pk=job_id)
    ids = list(SplitJob.objects.values_list("id", flat=True))
    pos = ids.index(job.id)
    return render(request, "library/splitter_editor.html", {
        "job": job,
        "prev_id": ids[pos - 1] if pos > 0 else None,
        "next_id": ids[pos + 1] if pos + 1 < len(ids) else None,
    })


@staff_member_required
@require_http_methods(["GET", "PUT"])
def splitter_job_api(request, job_id):
    job = get_object_or_404(SplitJob, pk=job_id)
    if request.method == "PUT":
        try:
            body = json.loads(request.body)
            job.tracks = validate_tracks(body.get("tracks"), job.duration)
        except (json.JSONDecodeError, AttributeError):
            return JsonResponse({"error": "Request body must be JSON with a tracks list."}, status=400)
        except SplitError as e:
            return JsonResponse({"error": str(e)}, status=400)
        job.save(update_fields=["tracks", "updated_at"])
    return JsonResponse({
        **_job_summary(job),
        "duration": job.duration,
        "album_artist": job.album_artist,
        "dest_folder": job.dest_folder,
        "playlist": job.playlist,
        "tracks": job.tracks,
        "segments": job.segments,
        "output_files": job.output_files,
    })


@staff_member_required
@require_GET
def splitter_waveform(request, job_id):
    job = get_object_or_404(SplitJob, pk=job_id)
    if not job.waveform:
        try:
            job.waveform, job.duration = compute_waveform(job)
        except SplitError as e:
            return JsonResponse({"error": str(e)}, status=500)
        job.save(update_fields=["waveform", "duration"])
    return JsonResponse({"resolution": job.waveform_resolution, "peaks": job.waveform, "duration": job.duration})


@staff_member_required
@require_GET
def splitter_audio(request, job_id):
    job = get_object_or_404(SplitJob, pk=job_id)
    if not Path(job.source_path).is_file():
        raise Http404("Source recording is missing on disk")
    try:
        path = ensure_playback_proxy(job)
    except SplitError as e:
        return JsonResponse({"error": str(e)}, status=500)
    return ranged_file_response(request, path)


@staff_member_required
@require_POST
def splitter_import(request, job_id):
    job = get_object_or_404(SplitJob, pk=job_id)
    try:
        written = cut_and_import(job)
    except SplitError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:  # surface anything unexpected (ffmpeg, DB) to the editor
        return JsonResponse({"error": f"Import failed: {type(e).__name__}: {e}"}, status=500)
    return JsonResponse({"imported": len(written), "files": written, **_job_summary(job)})

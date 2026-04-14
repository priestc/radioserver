from django.db.models.signals import post_delete
from django.dispatch import receiver


@receiver(post_delete, sender="library.Track")
def delete_empty_album(sender, instance, **kwargs):
    """Delete the album when its last track is removed."""
    album = instance.album
    if album is not None and not album.tracks.exists():
        album.delete()

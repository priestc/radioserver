from django.contrib import admin
from django.urls import include, path

from library import views as library_views

urlpatterns = [
    path("", library_views.search_landing_page, name="search_landing"),
    path("admin/", admin.site.urls),
    path("library/", include("library.urls")),
]

from django.urls import path

from activity import views

urlpatterns = [
    path("activity/", views.activity_index, name="activity"),
    path("api/v1/activity.json", views.activity_json, name="activity-json"),
]

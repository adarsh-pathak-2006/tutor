from django.contrib import admin, messages

from apps.core.admin_guards import VersionStatusForm
from apps.core.errors import ApiError

from . import services
from .models import Answer, Attempt, AttemptItem, Question, QuestionVersion


class QuestionVersionInline(admin.StackedInline):
    model = QuestionVersion
    form = VersionStatusForm
    extra = 0
    fields = ("version_no", "status", "body", "is_ai_draft", "author", "reviewer", "published_at")
    readonly_fields = ("published_at", "published_by", "created_at", "updated_at")
    raw_id_fields = ("author", "reviewer")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and getattr(obj, "status", None) in (QuestionVersion.Status.PUBLISHED, QuestionVersion.Status.ARCHIVED):
            fields += ["body", "version_no", "is_ai_draft"]
        return fields

    def has_delete_permission(self, request, obj=None):
        if obj and getattr(obj, "status", None) in (QuestionVersion.Status.PUBLISHED, QuestionVersion.Status.ARCHIVED):
            return False
        return super().has_delete_permission(request, obj)


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "lesson", "skill", "type", "difficulty", "purpose", "position")
    list_filter = ("purpose", "type", "difficulty", "lesson__module__course")
    search_fields = ("skill__code",)
    inlines = [QuestionVersionInline]


@admin.register(QuestionVersion)
class QuestionVersionAdmin(admin.ModelAdmin):
    form = VersionStatusForm
    list_display = ("__str__", "question", "status", "is_ai_draft", "author", "reviewer")
    list_filter = ("status", "is_ai_draft")
    raw_id_fields = ("question", "author", "reviewer", "published_by")
    readonly_fields = ("published_at", "published_by", "created_at", "updated_at")
    actions = ["publish_selected"]

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and getattr(obj, "status", None) in (QuestionVersion.Status.PUBLISHED, QuestionVersion.Status.ARCHIVED):
            fields += ["body", "question", "version_no", "is_ai_draft"]
        return fields

    def has_delete_permission(self, request, obj=None):
        if obj and getattr(obj, "status", None) in (QuestionVersion.Status.PUBLISHED, QuestionVersion.Status.ARCHIVED):
            return False
        return super().has_delete_permission(request, obj)

    @admin.action(description="Publish selected approved versions")
    def publish_selected(self, request, queryset):
        done = 0
        for version in queryset:
            try:
                services.publish_question_version(version, request.user)
                done += 1
            except ApiError as exc:
                self.message_user(request, f"{version}: {exc.message}", level=messages.WARNING)
        if done:
            self.message_user(request, f"Published {done} version(s).")


class AttemptItemInline(admin.TabularInline):
    model = AttemptItem
    extra = 0
    readonly_fields = ("question_version", "position", "hints_used")
    can_delete = False


@admin.register(Attempt)
class AttemptAdmin(admin.ModelAdmin):
    list_display = ("student", "lesson", "kind", "score", "max_score", "started_at", "submitted_at")
    list_filter = ("kind",)
    raw_id_fields = ("student", "lesson")
    inlines = [AttemptItemInline]


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ("item", "is_correct", "marks", "marked_by", "answered_at")
    list_filter = ("is_correct", "marked_by")

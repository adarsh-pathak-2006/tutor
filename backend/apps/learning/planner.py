"""The 'Today' screen (journey S8): chosen from the learner record, never at random."""

from datetime import timedelta

from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.assessment.models import Answer
from apps.catalogue.models import Lesson, PublishStatus

from .access import entitled_course_ids
from .models import LessonProgress, MasteryState


def accessible_lessons(student, course_ids=None):
    """Lessons in entitled courses, in course order (school chapter order within a course)."""
    if course_ids is None:
        course_ids = entitled_course_ids(student)

    return (
        Lesson.objects.filter(
            module__course_id__in=course_ids,
            module__course__status=PublishStatus.PUBLISHED,
        )
        .select_related("module__course")
        .order_by("module__course__title", "module__position", "position")
    )


def next_lesson(student, course_ids=None):
    """The first accessible lesson the student hasn't finished. Filtered in the database, one row fetched."""
    finished = LessonProgress.objects.filter(student=student, status=LessonProgress.Status.FINISHED).values("lesson_id")

    return accessible_lessons(student, course_ids=course_ids).exclude(id__in=finished).first()


def review_item(student, course_ids=None):
    """The weakest practised skill, and a lesson that teaches it."""
    weakest = (
        MasteryState.objects.filter(student=student, evidence_count__gt=0)
        .exclude(level=MasteryState.Level.MASTERED)
        .select_related("skill")
        .order_by("score")
        .first()
    )
    if not weakest:
        return None

    lesson = accessible_lessons(student, course_ids=course_ids).filter(skill_links__skill=weakest.skill).first()

    return {"skill": weakest.skill, "state": weakest, "lesson": lesson}


STREAK_WINDOW_DAYS = 366  # a longer streak is shown as this many days; keeps the query bounded


def streak_days(student) -> int:
    """Consecutive days, ending today, with at least one answer or finished lesson.

    Days are counted in IST (TIME_ZONE), not UTC: 1 a.m. in Kolkata is still "today" for the student.
    The database returns distinct dates only, never one row per answer.
    """
    today = timezone.localdate()
    since = timezone.now() - timedelta(days=STREAK_WINDOW_DAYS)
    tz = timezone.get_current_timezone()
    answer_days = (
        Answer.objects.filter(item__attempt__student=student, answered_at__gte=since)
        .annotate(day=TruncDate("answered_at", tzinfo=tz))
        .order_by()  # model ordering would break DISTINCT
        .values_list("day", flat=True)
        .distinct()
    )
    lesson_days = (
        LessonProgress.objects.filter(student=student, finished_at__gte=since)
        .annotate(day=TruncDate("finished_at", tzinfo=tz))
        .order_by()  # model ordering would break DISTINCT
        .values_list("day", flat=True)
        .distinct()
    )
    days = set(answer_days) | set(lesson_days)
    streak = 0
    day = today
    while day in days:
        streak += 1
        day -= timedelta(days=1)
    return streak

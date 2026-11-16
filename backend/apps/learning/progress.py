from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.catalogue.models import Lesson, Skill
from apps.content.models import ContentVersion

from .models import CourseCompletion, LessonProgress, MasteryState

DONE_LEVELS = {MasteryState.Level.DEVELOPING, MasteryState.Level.MASTERED}


def mark_opened(student, lesson):
    LessonProgress.objects.get_or_create(student=student, lesson=lesson)


def mark_finished(student, lesson):
    progress, _ = LessonProgress.objects.get_or_create(student=student, lesson=lesson)
    if progress.status != LessonProgress.Status.FINISHED:
        progress.status = LessonProgress.Status.FINISHED
        progress.finished_at = timezone.now()
        progress.save(update_fields=["status", "finished_at"])


def check_course_completion(student, course) -> bool:
    """C4: every lesson finished AND every skill the course teaches at least 'developing'."""
    if CourseCompletion.objects.filter(student=student, course=course).exists():
        return True

    lessons = Lesson.objects.filter(
        module__course=course,
        versions__status=ContentVersion.Status.PUBLISHED,
    ).distinct()
    if not lessons.exists():
        return False

    unfinished = lessons.annotate(
        is_finished=Exists(
            LessonProgress.objects.filter(
                student=student,
                lesson_id=OuterRef("pk"),
                status=LessonProgress.Status.FINISHED,
            )
        )
    ).filter(is_finished=False)

    if unfinished.exists():
        return False

    missing_mastery = (
        Skill.objects.filter(
            lesson_links__lesson__in=lessons,
            lesson_links__role="teaches",
        )
        .distinct()
        .annotate(
            has_good_mastery=Exists(
                MasteryState.objects.filter(
                    student=student,
                    skill_id=OuterRef("pk"),
                    level__in=DONE_LEVELS,
                )
            )
        )
        .filter(has_good_mastery=False)
    )

    if missing_mastery.exists():
        return False

    CourseCompletion.objects.get_or_create(student=student, course=course)
    return True

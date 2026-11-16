"""Student learning API — API_CONTRACTS.md §2.6. Every endpoint: logged in + active parental consent."""

from uuid import UUID

from ninja import Router
from ninja.security import django_auth

from apps.assessment.models import Attempt
from apps.assessment.services import published_quiz_versions
from apps.catalogue.api import lessons_router  # lesson endpoints share the /lessons prefix
from apps.catalogue.models import Lesson, Skill
from apps.content.services import published_version
from apps.core.errors import ApiError

from . import planner, progress
from .access import entitled_course_ids, require_active_student, require_lesson_access
from .models import CourseCompletion, LessonProgress, MasteryState
from .schemas import FinishOut, LessonOut, RecordOut, TodayOut

student_router = Router(tags=["learning"], auth=django_auth)


def get_lesson_for_student(user, lesson_id):
    require_active_student(user)
    lesson = Lesson.objects.select_related("module__course").filter(pk=lesson_id).first()
    if not lesson:
        raise ApiError(404, "not_found", "Lesson not found.")
    require_lesson_access(user, lesson)
    return lesson


@lessons_router.get("/{lesson_id}", response=LessonOut, auth=django_auth, tags=["learning"])
def open_lesson(request, lesson_id: UUID):
    lesson = get_lesson_for_student(request.user, lesson_id)
    version = published_version(lesson)
    if not version:
        raise ApiError(404, "not_found", "This lesson has no published content yet.")
    progress.mark_opened(request.user, lesson)
    has_quiz = published_quiz_versions(lesson).exists()
    skills = Skill.objects.filter(lesson_links__lesson=lesson).order_by("code")
    return {
        "id": lesson.id,
        "title": lesson.title,
        "course_slug": lesson.module.course.slug,
        "course_title": lesson.module.course.title,
        "module_title": lesson.module.title,
        "version_no": version.version_no,
        "sections": version.sections,
        "skills": [{"code": s.code, "name": s.name} for s in skills],
        "has_quiz": has_quiz,
    }


@lessons_router.post("/{lesson_id}/finish", response=FinishOut, auth=django_auth, tags=["learning"])
def finish_lesson(request, lesson_id: UUID):
    lesson = get_lesson_for_student(request.user, lesson_id)
    version = published_version(lesson)
    if not version:
        raise ApiError(404, "not_found", "This lesson has no published content yet.")
    progress.mark_finished(request.user, lesson)
    completed = progress.check_course_completion(request.user, lesson.module.course)
    nxt = planner.next_lesson(request.user)
    return {"finished": True, "course_completed": completed, "next_lesson_id": nxt.id if nxt else None}


@student_router.get("/today", response=TodayOut)
def today(request):
    require_active_student(request.user)

    course_ids = entitled_course_ids(request.user)
    nxt = planner.next_lesson(request.user, course_ids=course_ids)
    review = planner.review_item(request.user, course_ids=course_ids)
    return {
        "next_lesson": {"id": nxt.id, "title": nxt.title, "course_title": nxt.module.course.title} if nxt else None,
        "review": {
            "skill_code": review["skill"].code,
            "skill_name": review["skill"].name,
            "level": review["state"].level,
            "lesson_id": review["lesson"].id if review["lesson"] else None,
        }
        if review
        else None,
        "next_session": None,
        "streak_days": planner.streak_days(request.user),
    }


@student_router.get("/record", response=RecordOut)
def record(request):
    """The student's own learner record. (Parents get their own view in the parent API.)"""
    require_active_student(request.user)
    user = request.user
    states = MasteryState.objects.filter(student=user).select_related("skill__subject").order_by("skill__code")
    completions = CourseCompletion.objects.filter(student=user).select_related("course").order_by("-completed_at")
    attempts = (
        Attempt.objects.filter(student=user, submitted_at__isnull=False)
        .select_related("lesson")
        .order_by("-submitted_at")[:50]
    )
    return {
        "mastery": [
            {
                "code": s.skill.code,
                "name": s.skill.name,
                "subject": s.skill.subject.slug,
                "level": s.level,
                "score": s.score,
                "evidence": s.evidence_count,
            }
            for s in states
        ],
        "completed_courses": [
            {"slug": c.course.slug, "title": c.course.title, "completed_at": c.completed_at} for c in completions
        ],
        "quizzes": [
            {
                "lesson_title": a.lesson.title if a.lesson else "",
                "score": a.score or 0,
                "max_score": a.max_score,
                "submitted_at": a.submitted_at,
            }
            for a in attempts
        ],
        "lessons_finished": LessonProgress.objects.filter(student=user, status="finished").count(),
    }

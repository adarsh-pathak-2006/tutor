from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('assessment', '0001_initial'),
        ('catalogue', '0002_course_module_lesson_course_free_module_programme_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='attempt',
            constraint=models.UniqueConstraint(condition=models.Q(('submitted_at__isnull', True)), fields=('student', 'lesson', 'kind'), name='unique_open_attempt_per_student_lesson_kind'),
        ),
    ]

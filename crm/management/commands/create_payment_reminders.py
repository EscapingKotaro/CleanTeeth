from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Coalesce, F, Sum
from django.utils import timezone

from crm.models import PaymentSchedule, Task, log_action, AuditLog


class Command(BaseCommand):
    help = "Создаёт кураторам задачи-напоминания по наступившим датам платежей"

    def handle(self, *args, **options):
        today = timezone.now().date()
        items = (
            PaymentSchedule.objects
            .annotate(paid=Coalesce(Sum("payments__amount"), Decimal("0")))
            .filter(due_date__lte=today, reminder_task__isnull=True)
            .exclude(paid__gte=F("planned_amount"))
            .select_related("plan")
        )
        created = 0
        for item in items:
            case = item.plan.cases.select_related("curator").first()
            if not case or not case.is_active:
                continue
            task = Task.objects.create(
                case=case,
                task_type=Task.TaskType.PAYMENT,
                description=(
                    f"Напомнить об оплате {item.planned_amount} ₽ "
                    f"(плановая дата {item.due_date:%d.%m.%Y}"
                    f"{', аванс' if item.is_advance else ''})"
                ),
                due_date=timezone.now(),
                assignee=case.curator,
                priority=Task.Priority.HIGH if item.is_overdue else Task.Priority.MEDIUM,
            )
            item.reminder_task = task
            item.save(update_fields=["reminder_task"])
            log_action(
                action=AuditLog.Action.CREATE, instance=task,
                field_name="task", new_value=task.description,
                comment="Авто-напоминание по наступившей дате платежа",
            )
            created += 1
        self.stdout.write(self.style.SUCCESS(f"Создано задач-напоминаний: {created}"))
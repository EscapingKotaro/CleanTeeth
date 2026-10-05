from django.urls import path
from . import views

app_name = "crm"

urlpatterns = [
    # ═══════════ АУТЕНТИФИКАЦИЯ (вне ролей) ═══════════
    path("login/", views.CustomLoginView.as_view(), name="login"),
    path("logout/", views.CustomLogoutView.as_view(), name="logout"),
    path("password-change/", views.CustomPasswordChangeView.as_view(), name="password_change"),
    path("password-change/done/", views.CustomPasswordChangeDoneView.as_view(), name="password_change_done"),

    # ═══════════ ВХОД: редирект по роли ═══════════
    path("", views.role_home, name="role_home"),

    # ═══════════ ЗОНА /my/ — личный кабинет (любая роль, свои данные) ═══════════
    path("my/home/", views.home, name="home"),
    path("my/day/", views.day_plan, name="day_plan"),
    path("my/tasks/", views.tasks_list, name="tasks_list"),
    path("my/motivation/", views.motivation, name="motivation"),

    # ═══════════ ЗОНА РАБОТЫ: кейсы, планы, финансы (все роли, доступ по владению кейсом) ═══════════
    path("cases/", views.cases_list, name="cases_list"),
    path("cases/<int:case_id>/", views.case_detail, name="case_detail"),
    path("cases/<int:case_id>/status/", views.case_change_status, name="case_change_status"),
    path("cases/<int:case_id>/task/create/", views.task_create, name="task_create"),
    path("tasks/<int:task_id>/complete/", views.task_complete, name="task_complete"),
    path("tasks/<int:task_id>/postpone/", views.task_postpone, name="task_postpone"),

    path("plans/create/", views.plan_create, name="plan_create"),
    path("plans/<int:plan_id>/update/", views.plan_update, name="plan_update"),
    path("plans/<int:plan_id>/sign/", views.plan_sign, name="plan_sign"),
    path("plans/<int:plan_id>/directions/", views.plan_directions_update, name="plan_directions_update"),
    path("plans/<int:plan_id>/schedule/create/", views.schedule_item_create, name="schedule_item_create"),
    path("plans/<int:plan_id>/schedule/sign/", views.schedule_sign, name="schedule_sign"),
    path("plans/<int:plan_id>/payment/create/", views.payment_create, name="payment_create"),
    path("schedule/<int:item_id>/update/", views.schedule_item_update, name="schedule_item_update"),
    path("schedule/<int:item_id>/delete/", views.schedule_item_delete, name="schedule_item_delete"),

    # ═══════════ ПАЦИЕНТЫ И ДОГОВОРЫ (все роли) ═══════════
    path("patients/", views.patients_board, name="patients_board"),          # доска сопровождений
    path("patients/manage/", views.patients_list, name="patients_list"),     # справочник/CRUD
    path("patients/manage/create/", views.patient_create, name="patient_create"),
    path("patients/manage/<int:patient_id>/update/", views.patient_update, name="patient_update"),
    path("patients/manage/<int:patient_id>/delete/", views.patient_delete, name="patient_delete"),
    path("patients/<int:patient_id>/contracts/create/", views.contract_create, name="contract_create"),
    path("contracts/<int:contract_id>/update/", views.contract_update, name="contract_update"),

    # ═══════════ СПРАВОЧНИК ВРАЧЕЙ (все роли) ═══════════
    path("doctors/", views.doctors_list, name="doctors_list"),
    path("doctors/create/", views.doctor_create, name="doctor_create"),
    path("doctors/<int:doctor_id>/update/", views.doctor_update, name="doctor_update"),
    path("doctors/<int:doctor_id>/delete/", views.doctor_delete, name="doctor_delete"),

    # ═══════════ ДОКУМЕНТЫ: смотрят все, проверяет управляющая ═══════════
    path("doc-checks/", views.doc_checks, name="doc_checks"),
    path("doc-checks/create/", views.doc_check_create, name="doc_check_create"),

    # ═══════════ ЗОНА /manager/ — руководители (старший куратор и выше) ═══════════
    path("manager/reports/", views.manager_report, name="manager_report"),
    path("manager/team-control/", views.team_control, name="team_control"),
    path("manager/analytics/", views.analytics, name="analytics"),
    path("manager/motivation/plan/upsert/", views.monthly_plan_upsert, name="monthly_plan_upsert"),
    path("manager/users/", views.users_list, name="users_list"),
    path("manager/users/create/", views.user_create, name="user_create"),
    path("manager/users/<int:user_id>/update/", views.user_update, name="user_update"),
    path("manager/users/<int:user_id>/toggle-active/", views.user_toggle_active, name="user_toggle_active"),

    # ═══════════ ЗОНА /service/ — администрирование (только ADMIN) ═══════════
    path("service/settings/", views.settings_view, name="settings"),
]

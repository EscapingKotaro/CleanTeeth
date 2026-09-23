from django.urls import path
from . import views

app_name = "crm"

urlpatterns = [
    # Аутентификация
    path("login/", views.CustomLoginView.as_view(), name="login"),
    path("logout/", views.CustomLogoutView.as_view(), name="logout"),
    path("password-change/", views.CustomPasswordChangeView.as_view(), name="password_change"),
    path("password-change/done/", views.CustomPasswordChangeDoneView.as_view(), name="password_change_done"),

    # CRM страницы
    path("", views.dashboard, name="dashboard"),
    path("cases/", views.cases_list, name="cases_list"),  # ← было patients_list
    path("cases/<int:case_id>/", views.case_detail, name="case_detail"),  # ← новое
    path("tasks/", views.tasks_list, name="tasks_list"),
    path("motivation/", views.motivation, name="motivation"),
    path("team-control/", views.team_control, name="team_control"),
    path("analytics/", views.analytics, name="analytics"),
    path("document-checks/", views.document_checks, name="document_checks"),
    path("settings/", views.settings_view, name="settings"),

    path("patient/create/", views.patient_create, name="patient_create"),
    path("patient/<int:patient_id>/update/", views.patient_update, name="patient_update"),
    path("plan/<int:plan_id>/update/", views.plan_update, name="plan_update"),
    path("plan/create/", views.plan_create, name="plan_create"),
]

import re

from django import forms
from django.forms import BaseFormSet, formset_factory
from django.contrib.auth import get_user_model

from .file_monitors import get_file_monitor_limit_for_tenant, validate_file_monitor_specs
from .models import AlloyVersion, Node, NodeFeatures, SupervisorVersion, TenantUser

FILE_MONITOR_FORMSET_PREFIX = "file_monitors"
SEMVER_RE = re.compile(
    r"^v?"
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _parse_semver(value):
    match = SEMVER_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    major, minor, patch, prerelease = match.groups()
    return int(major), int(minor), int(patch), prerelease.split(".") if prerelease else []


def _compare_prerelease(left, right):
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1

    for left_part, right_part in zip(left, right):
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            left_value = int(left_part)
            right_value = int(right_part)
            if left_value != right_value:
                return 1 if left_value > right_value else -1
        elif left_numeric != right_numeric:
            return -1 if left_numeric else 1
        elif left_part != right_part:
            return 1 if left_part > right_part else -1

    if len(left) == len(right):
        return 0
    return 1 if len(left) > len(right) else -1


def _compare_semver(left, right):
    left_parsed = _parse_semver(left)
    right_parsed = _parse_semver(right)
    if not left_parsed or not right_parsed:
        return None

    left_core = left_parsed[:3]
    right_core = right_parsed[:3]
    if left_core != right_core:
        return 1 if left_core > right_core else -1
    return _compare_prerelease(left_parsed[3], right_parsed[3])


def _is_upgrade(candidate_version, current_version):
    if not current_version:
        return True
    if not _parse_semver(current_version):
        return True
    comparison = _compare_semver(candidate_version, current_version)
    return comparison is not None and comparison > 0


def _upgrade_only_queryset(queryset, current_version):
    if not current_version or not _parse_semver(current_version):
        return queryset
    allowed_ids = [
        item.pk
        for item in queryset
        if _is_upgrade(item.version, current_version)
    ]
    return queryset.filter(pk__in=allowed_ids)


class NodeForm(forms.ModelForm):
    class Meta:
        model = Node
        fields = ["node_name", "enabled"]


class NodeUpdateForm(forms.ModelForm):
    target_supervisor_version = forms.ModelChoiceField(
        queryset=SupervisorVersion.objects.none(),
        required=False,
        empty_label="No supervisor update",
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    target_alloy_version = forms.ModelChoiceField(
        queryset=AlloyVersion.objects.none(),
        required=False,
        empty_label="No Alloy update",
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )

    class Meta:
        model = Node
        fields = ["target_supervisor_version", "target_alloy_version"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        supervisor_queryset = SupervisorVersion.objects.filter(
            enabled=True,
            artifacts__enabled=True,
        ).distinct().order_by("-created_at", "version")
        alloy_queryset = AlloyVersion.objects.filter(
            enabled=True,
            artifacts__enabled=True,
        ).distinct().order_by("-created_at", "version")
        self.fields["target_supervisor_version"].queryset = _upgrade_only_queryset(
            supervisor_queryset,
            self.instance.supervisor_version,
        )
        self.fields["target_alloy_version"].queryset = _upgrade_only_queryset(
            alloy_queryset,
            self.instance.alloy_version,
        )
        self.fields["target_supervisor_version"].help_text = (
            "Only versions newer than the reported supervisor version are available."
        )
        self.fields["target_alloy_version"].help_text = (
            "Only versions newer than the reported Alloy version are available."
        )

    def clean_target_supervisor_version(self):
        target = self.cleaned_data.get("target_supervisor_version")
        if target and not _is_upgrade(target.version, self.instance.supervisor_version):
            raise forms.ValidationError("Choose a supervisor version newer than the current reported version.")
        return target

    def clean_target_alloy_version(self):
        target = self.cleaned_data.get("target_alloy_version")
        if target and not _is_upgrade(target.version, self.instance.alloy_version):
            raise forms.ValidationError("Choose an Alloy version newer than the current reported version.")
        return target


class NodeCreateForm(forms.ModelForm):
    class Meta:
        model = Node
        fields = ["node_name"]


class NodeFeaturesForm(forms.ModelForm):
    class Meta:
        model = NodeFeatures
        fields = [
            "linuxMonitor", "containerMonitor", "journalLog", "dockerLogs", "fileMonitor",
        ]
        widgets = {
            f: forms.CheckboxInput(attrs={"class": "toggle toggle-primary"})
            for f in [
                "linuxMonitor", "containerMonitor", "journalLog", "dockerLogs", "fileMonitor",
            ]
        }

    def clean(self):
        cleaned = super().clean()

        linux_fields = [
            "linuxMonitor", "containerMonitor", "journalLog",
            "dockerLogs", "fileMonitor",
        ]
        linux_on = any(cleaned.get(f) for f in linux_fields)

        if not linux_on:
            raise forms.ValidationError("Enable at least one Linux monitoring or logging feature.")

        return cleaned


class FileMonitorForm(forms.Form):
    id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    label = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full", "placeholder": "Application log"}),
    )
    file_path = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full", "placeholder": "/var/log/app.log"}),
    )
    enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
    )


class BaseFileMonitorFormSet(BaseFormSet):
    def __init__(self, *args, tenant=None, feature_enabled=False, **kwargs):
        self.tenant = tenant
        self.feature_enabled = feature_enabled
        self.cleaned_specs = []
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        specs = []
        for form in self.forms:
            if self.can_delete and self._should_delete_form(form):
                continue
            cleaned = form.cleaned_data
            specs.append(
                {
                    "id": cleaned.get("id"),
                    "label": cleaned.get("label"),
                    "file_path": cleaned.get("file_path"),
                    "enabled": cleaned.get("enabled"),
                }
            )

        try:
            self.cleaned_specs = validate_file_monitor_specs(
                tenant=self.tenant,
                specs=specs,
                require_enabled=self.feature_enabled,
            )
        except forms.ValidationError as exc:
            raise forms.ValidationError(exc.messages)


def has_file_monitor_formset_data(data):
    return data is not None and f"{FILE_MONITOR_FORMSET_PREFIX}-TOTAL_FORMS" in data


def build_file_monitor_formset(*, data=None, tenant, node=None, feature_enabled=False):
    limit = get_file_monitor_limit_for_tenant(tenant)
    existing = list(node.file_monitors.order_by("id")) if node else []
    initial = [
        {
            "id": monitor.pk,
            "label": monitor.label,
            "file_path": monitor.file_path,
            "enabled": monitor.enabled,
        }
        for monitor in existing
    ]
    extra = max(limit - len(initial), 0)
    FileMonitorFormSet = formset_factory(
        FileMonitorForm,
        formset=BaseFileMonitorFormSet,
        can_delete=True,
        extra=extra,
        max_num=max(limit, len(initial)),
        validate_max=False,
    )
    formset = FileMonitorFormSet(
        data=data,
        initial=initial,
        prefix=FILE_MONITOR_FORMSET_PREFIX,
        tenant=tenant,
        feature_enabled=feature_enabled,
    )
    formset.file_monitor_limit = limit
    return formset



class TenantUserForm(forms.ModelForm):
    email = forms.EmailField(
        label="Email",
        required=False,
        widget=forms.EmailInput(attrs={"class": "input input-bordered w-full", "autocomplete": "email"}),
    )

    class Meta:
        model = TenantUser
        fields = ["email", "role"]
        widgets = {
            "role": forms.Select(attrs={"class": "select select-bordered w-full"}),
        }

    def __init__(self, *args, **kwargs):
        self.tenant = kwargs.pop("tenant", None)
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["email"].initial = self.instance.user.email
            self.fields["email"].required = False
            self.fields["email"].widget.attrs["readonly"] = True

    def clean_email(self):
        if self.instance and self.instance.pk:
            self._resolved_user = self.instance.user
            return self.instance.user.email

        email = self.cleaned_data.get("email", "").strip()
        username = self.data.get("username", "").strip()
        User = get_user_model()
        try:
            if email:
                user = User.objects.get(email__iexact=email)
            elif username:
                user = User.objects.get(username__iexact=username)
            else:
                raise forms.ValidationError("Email is required.")
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            raise forms.ValidationError("Unable to add this account. Please check the email address or username.")
        self._resolved_user = user
        return user.email or email

    def clean(self):
        cleaned = super().clean()
        user = getattr(self, "_resolved_user", None)

        if not self.tenant:
            raise forms.ValidationError("No tenant context was provided.")

        if user:
            qs = TenantUser.objects.exclude(pk=self.instance.pk)

            # 1) Same-tenant duplicate
            exists_same = qs.filter(tenant=self.tenant, user=user).exists()
            if exists_same:
                # Attach to field so the message shows right under the email input.
                self.add_error("email", "This account is already in your team.")
                raise forms.ValidationError("This account is already in your team.")

            # Multiple tenants per user are allowed.

        return cleaned

    def save(self, commit=True):
        if hasattr(self, "_resolved_user"):
            self.instance.user = self._resolved_user
        if self.tenant:
            self.instance.tenant = self.tenant
        return super().save(commit=commit)

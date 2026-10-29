from django import forms

from clusters.models import Cluster
from hosts.models import host_name_validator


class ClusterNameForm(forms.Form):
    """Rename a cluster. Unique per owner."""

    name = forms.CharField(
        max_length=64,
        validators=[host_name_validator],
        widget=forms.TextInput(attrs={"class": "well", "autocomplete": "off"}),
        help_text="1–64 characters: letters, digits, '-' and '_'.",
    )

    def __init__(self, *args, owner, instance: Cluster | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner
        self.instance = instance

    def clean_name(self) -> str:
        name = self.cleaned_data["name"]
        taken = Cluster.objects.filter(owner=self.owner, name=name)
        if self.instance is not None:
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise forms.ValidationError("You already have a cluster with this name.")
        return name


class DeleteClusterForm(forms.Form):
    """Deleting a cluster requires typing its name, so it can't happen by accident."""

    confirm_name = forms.CharField(
        max_length=64,
        label="Type the cluster name to confirm",
        widget=forms.TextInput(attrs={"class": "well", "autocomplete": "off"}),
    )

    def __init__(self, *args, cluster: Cluster, **kwargs):
        super().__init__(*args, **kwargs)
        self.cluster = cluster

    def clean_confirm_name(self) -> str:
        value = self.cleaned_data["confirm_name"]
        if value != self.cluster.name:
            raise forms.ValidationError("The name doesn't match.")
        return value

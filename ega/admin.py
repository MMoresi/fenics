from django.contrib import admin

from ega.models import (
    EgaUser,
    League,
    Match,
    Prediction,
    Team,
    TeamStats,
    Tournament,
)


admin.site.register(EgaUser)
admin.site.register(League)
admin.site.register(Match)
admin.site.register(Prediction)
admin.site.register(Team)
admin.site.register(TeamStats)
admin.site.register(Tournament)

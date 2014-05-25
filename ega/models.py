# -*- coding: utf-8 -*-

import random
import string

from datetime import datetime, timedelta
from functools import partial

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.mail import send_mail
from django.db import models
from django.db.models import Count, F, Q, Sum
from django.db.models.signals import post_save
from django.dispatch import receiver

from ega import settings as game_settings
from ega.constants import (
    EL_EGA_NO_REPLY,
    INVITE_BODY,
    INVITE_SUBJECT,
    LEAGUE_JOIN_CHOICES,
)
from ega.managers import PredictionManager


ALNUM_CHARS = string.letters + string.digits


def rand_str(length=8):
    return ''.join(random.choice(ALNUM_CHARS) for x in xrange(length))


class EgaUser(AbstractUser):
    # TODO: add avatar

    invite_key = models.CharField(
        max_length=10, default=partial(rand_str, 20), unique=True)

    def invite_friends(self, emails, subject=None, body=None):
        if subject is None:
            subject = INVITE_SUBJECT
        if body is None:
            body = INVITE_BODY
        send_mail(subject, body, EL_EGA_NO_REPLY, emails)

    def stats(self, tournament):
        """User stats for given tournament."""
        stats = {}
        ranking = Prediction.objects.filter(
            match__tournament=tournament, user=self, score__gt=0)
        stats['winners'] = len(ranking)
        stats['score'] = sum(r.score for r in ranking)
        stats['exacts'] = sum(1 for r in ranking
                              if r.score == game_settings.EXACTLY_MATCH_POINTS)
        return stats


class Tournament(models.Model):
    """Tournament metadata."""
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    teams = models.ManyToManyField('Team')
    published = models.BooleanField(default=False)

    def __unicode__(self):
        return self.name

    def next_matches(self, days=game_settings.NEXT_MATCHES_DAYS):
        """Return matches in the next days."""
        now = datetime.utcnow()
        until = now + timedelta(days=days)
        return self.match_set.filter(when__range=(now, until))

    def ranking(self):
        """Users ranking in the tournament."""
        ranking = Prediction.objects.filter(
            match__tournament=self).values('user__username').annotate(
                total=Sum('score'), count=Count('id')).order_by('-total')
        return ranking


class Team(models.Model):
    """Team metadata."""
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    image = models.ImageField(upload_to='teams', null=True, blank=True)

    def __unicode__(self):
        return self.name

    def latest_matches(self, tournament=None):
        """Return team previously played matches."""
        now = datetime.now()
        matches = Match.objects.filter(
            Q(away=self)|Q(home=self), when__lte=now)
        return matches


class Match(models.Model):
    """Match metadata."""
    home = models.ForeignKey(Team, related_name='home_games')
    away = models.ForeignKey(Team, related_name='away_games')
    home_goals = models.IntegerField(null=True, blank=True)
    away_goals = models.IntegerField(null=True, blank=True)

    tournament = models.ForeignKey('Tournament')
    when = models.DateTimeField(null=True, blank=True)
    location = models.CharField(max_length=200, blank=True)
    referee = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ('when',)

    def __unicode__(self):
        return u"%s: %s vs %s" % (
            self.tournament, self.home.name, self.away.name)

    @property
    def deadline(self):
        """Return deadline datetime or None if match date is not set."""
        ret = None
        if self.when:
            ret = self.when - timedelta(hours=game_settings.HOURS_TO_DEADLINE)
        return ret


class Prediction(models.Model):
    """User prediction for a match."""
    TRENDS = ('L', 'E', 'V')

    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    match = models.ForeignKey('Match')

    home_goals = models.PositiveIntegerField(null=True, blank=True)
    away_goals = models.PositiveIntegerField(null=True, blank=True)
    trend = models.CharField(max_length=1, editable=False)
    starred = models.BooleanField(default=False)

    score = models.PositiveIntegerField(default=0)

    objects = PredictionManager()

    class Meta:
        ordering = ('match__when',)
        unique_together = ('user', 'match')

    def __unicode__(self):
        return u"%s: %s" % (self.user, self.match)

    @property
    def home_team_stats(self):
        stats, _ = self.match.home.teamstats_set.get_or_create(
            tournament=self.match.tournament)
        return stats

    @property
    def away_team_stats(self):
        stats, _ = self.match.away.teamstats_set.get_or_create(
            tournament=self.match.tournament)
        return stats

    def save(self, *args, **kwargs):
        # set trend value before saving
        if self.home_goals is not None and self.away_goals is not None:
            if self.home_goals > self.away_goals:
                self.trend = self.TRENDS[0]
            elif self.home_goals < self.away_goals:
                self.trend = self.TRENDS[2]
            else:
                self.trend = self.TRENDS[1]
        super(Prediction, self).save(*args, **kwargs)


class TeamStats(models.Model):
    """Stats for a team in a given tournament."""

    team = models.ForeignKey(Team)
    tournament = models.ForeignKey(Tournament)

    won = models.PositiveIntegerField(default=0)
    tie = models.PositiveIntegerField(default=0)
    lost = models.PositiveIntegerField(default=0)

    points = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('-points',)

    def __unicode__(self):
        return u"%s - %s" % (self.team, self.tournament)

    def sync(self):
        """Update team stats for tournament."""
        home = Match.objects.filter(
            tournament=self.tournament, home=self.team,
            home_goals__isnull=False, away_goals__isnull=False)
        away = Match.objects.filter(
            tournament=self.tournament, away=self.team,
            home_goals__isnull=False, away_goals__isnull=False)

        self.won = (
            home.filter(home_goals__gt=F('away_goals')).count() +
            away.filter(away_goals__gt=F('home_goals')).count())
        self.tie = (
            home.filter(home_goals=F('away_goals')).count() +
            away.filter(away_goals=F('home_goals')).count())
        self.lost = (
            home.filter(home_goals__lt=F('away_goals')).count() +
            away.filter(away_goals__lt=F('home_goals')).count())

        self.points = self._points()
        self.save()

    def _points(self):
        return (self.won * game_settings.MATCH_WON_POINTS +
                self.tie * game_settings.MATCH_TIE_POINTS + 
                self.lost * game_settings.MATCH_LOST_POINTS)


class League(models.Model):
    """Custom league metadata."""

    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=200, unique=True)
    tournament = models.ForeignKey('Tournament')
    created = models.DateTimeField(default=datetime.utcnow)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, through='LeagueMember')

    def __unicode__(self):
        return u"%s - %s" % (self.owner, self.name)

    def ranking(self):
        ranking = self.tournament.ranking().filter(user__in=self.members)
        return ranking


class LeagueMember(models.Model):
    """A league member."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    league = models.ForeignKey(League)
    is_owner = models.BooleanField()
    date_joined = models.DateTimeField(default=datetime.utcnow)
    origin = models.CharField(
        max_length=10, choices=LEAGUE_JOIN_CHOICES)

    class Meta:
        unique_together = ('user', 'league')


@receiver(post_save, sender=Match, dispatch_uid="update-scores")
def update_related_predictions(sender, instance, **kwargs):
    """Update score for predictions related to the changed match."""
    home_goals = instance.home_goals
    away_goals = instance.away_goals
    predictions = instance.prediction_set

    # update exact predictions
    predictions.filter(home_goals=home_goals, away_goals=away_goals).update(
        score=game_settings.EXACTLY_MATCH_POINTS)

    # update winner predictions
    if home_goals > away_goals:
        predictions.exclude(
            home_goals=home_goals, away_goals=away_goals).filter(
                home_goals__gt=F('away_goals')).update(
                    score=game_settings.WINNER_MATCH_POINTS)
    elif home_goals < away_goals:
        predictions.exclude(
            home_goals=home_goals, away_goals=away_goals).filter(
                home_goals__lt=F('away_goals')).update(
                    score=game_settings.WINNER_MATCH_POINTS)
    else:
        predictions.exclude(
            home_goals=home_goals, away_goals=away_goals).filter(
                home_goals=F('away_goals')).update(
                    score=game_settings.WINNER_MATCH_POINTS)

    # update starred predictions
    predictions.filter(score__gt=0, starred=True).update(score=F('score') + 1)


@receiver(post_save, sender=Match, dispatch_uid="update-stats")
def update_related_stats(sender, instance, **kwargs):
    """Update team stats related to the changed match."""
    for team in (instance.home, instance.away):
        stats, created = TeamStats.objects.get_or_create(
            team=team, tournament=instance.tournament)
        stats.sync()

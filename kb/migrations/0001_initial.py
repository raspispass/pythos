# -*- coding: utf-8 -*-
# Generated by Django 1.9.4 on 2016-04-06 04:19
from __future__ import unicode_literals

from django.db import migrations, models
import macaddress.fields


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='EtherOUI',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('oui', macaddress.fields.MACAddressField(blank=True, db_index=True, integer=True, null=True)),
                ('vendor', models.CharField(max_length=253)),
            ],
        ),
        migrations.CreateModel(
            name='OperatingSystem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('vendor', models.CharField(max_length=127)),
                ('product', models.CharField(max_length=127)),
                ('default_ttl', models.IntegerField()),
            ],
        ),
        migrations.CreateModel(
            name='ServiceName',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=127)),
                ('port', models.IntegerField(db_index=True)),
                ('protocol_l3', models.IntegerField(db_index=True)),
                ('description', models.TextField()),
            ],
        ),
        migrations.AlterUniqueTogether(
            name='servicename',
            unique_together=set([('port', 'protocol_l3')]),
        ),
    ]

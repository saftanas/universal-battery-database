# Generated by Django 2.2.11 on 2020-06-10 16:18

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('cell_database', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='MaterialType',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('notes', models.CharField(blank=True, max_length=100)),
            ],
        ),
        migrations.AddField(
            model_name='component',
            name='material_type_name',
            field=models.BooleanField(blank=True, default=False),
        ),
        migrations.AddField(
            model_name='component',
            name='material_type',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='cell_database.MaterialType'),
        ),
    ]
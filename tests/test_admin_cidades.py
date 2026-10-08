import json
from urllib.parse import parse_qs, urlsplit
import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from leads.models import Nicho, Prospect, Origem, Segmento
from leads.admin_cidades import ProspectCidadesFilter
pytestmark=pytest.mark.django_db
@pytest.fixture
def ambiente():
    n,_=Nicho.objects.get_or_create(codigo='beleza',defaults={'nome':'Beleza'})
    for i,cidade in enumerate(['Joinville','Blumenau','Itajaí','Joinville']):
        Prospect.objects.create(nome=f'Empresa {i}',cidade=cidade,nicho=n,origem=Origem.MANUAL,origem_id=f'multi-{i}',segmento=Segmento.OUTRO)
    c=Client();c.force_login(get_user_model().objects.create_superuser('multi',password='teste'))
    return c,reverse('admin:leads_prospect_changelist')
def test_uniao(ambiente):
    c,u=ambiente;r=c.get(u,{'cidades':json.dumps(['Joinville','Blumenau'])})
    assert r.status_code==200
    assert r.context['cl'].queryset.count()==3
    assert set(r.context['cl'].queryset.values_list('cidade',flat=True))=={'Joinville','Blumenau'}
def test_todas_e_legado(ambiente):
    c,u=ambiente
    assert c.get(u).context['cl'].queryset.count()==4
    assert c.get(u,{'cidade':'Joinville'}).context['cl'].queryset.count()==2
def test_links(ambiente):
    c,u=ambiente;r=c.get(u,{'cidades':json.dumps(['Joinville']),'q':'Empresa','segmento__exact':'OUTRO'})
    cl=r.context['cl'];f=next(x for x in cl.filter_specs if isinstance(x,ProspectCidadesFilter));op=list(f.choices(cl))
    assert len(op)==4
    p=parse_qs(urlsplit(next(x for x in op if 'Blumenau' in x['display'])['query_string']).query)
    assert set(json.loads(p['cidades'][0]))=={'Joinville','Blumenau'}
    assert p['q']==['Empresa'] and p['segmento__exact']==['OUTRO']
    assert 'cidades' not in parse_qs(urlsplit(next(x for x in op if 'Joinville' in x['display'])['query_string']).query)
def test_invalido(ambiente):
    c,u=ambiente;r=c.get(u,{'cidades':'[42]'})
    assert r.status_code==302 and 'e=1' in r.url
def test_virgula_acentos(ambiente):
    c,u=ambiente;p=Prospect.objects.first();p.cidade='Cidade, São';p.save()
    assert list(c.get(u,{'cidades':json.dumps(['Cidade, São'])}).context['cl'].queryset)==[p]

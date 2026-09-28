"""Pure recognition cases: synthetic catalog identities, never production assumptions."""
import pytest

import inventory_voice_fast as parser
import inventory_voice_products as products


def row(identifier, model, color, spacing):
    return {'id':identifier, 'sku':f'CAT-{identifier}-{color}-{spacing}', 'model':model,
            'name':f'{model} {color} {spacing}', 'ean':''}


@pytest.mark.parametrize('phrase,quantity', [
    ('mam cztery, poprawka pięć',5), ('mam dwie… nie, mam jeszcze siedem',9),
    ('mam 4 nie 5',5), ('mam dwie sztuki, jeszcze siedem sztuk',9),
])
def test_count_addition_and_replacement_are_distinct(phrase,quantity):
    command = parser.parse(phrase)
    assert command.kind == 'quantity'
    assert command.quantity == quantity


def test_ambiguous_quantity_keeps_product_but_cannot_be_written():
    command = parser.parse('Avery BB 160: 30 czy 36?')
    assert command.kind == 'product'
    assert command.product == 'avery bb 160'
    assert command.quantity is None
    assert command.quantity_options == (30,36)
    assert parser.parse('36 sztuk').quantity == 36


@pytest.mark.parametrize('query,model,color,spacing', [
    ('Sam BB 96','Sam','BB','96'), ('Carl BB 320','Carl','BB','320'),
    ('Karl BB 320','Carl','BB','320'), ('Andrę be be sto sześćdziesiąt','André','BB','160'),
    ('Noa BB 160','Noah','BB','160'), ('Windsor BB 160','Winsor','BB','160'),
    ('Dower BB 160','Dover','BB','160'), ('Dauber BB 160','Dover','BB','160'),
    ('BB 96 Sam','Sam','BB','96'), ('SamBB96','Sam','BB','96'),
])
def test_catalog_backed_names_colors_and_spacing_across_fields(query,model,color,spacing):
    catalog = [row(1,model,color,spacing),row(2,model,'BN',spacing),row(3,model,color,'196')]
    assert [item['id'] for item in products.resolve(catalog,query)] == [1]


def test_bare_spacing_matches_both_ranges_and_never_selects_one():
    catalog = [row(1,'Dover','BLB','192-224'),row(2,'Dover','BLB','224-256')]
    matched = products.resolve(catalog,'Dover BLB 224')
    assert [item['id'] for item in matched] == [1,2]
    assert all(item['_voice_requires_confirmation'] for item in matched)
    assert products.resolve(catalog,'Dover BLB 224',exact_only=True) == []
    assert products.resolve(catalog[:1],'Dover BLB 224')[0]['_voice_requires_confirmation']
    assert products.resolve(catalog,'Dover BLB 192-224')[0]['id'] == 1


@pytest.mark.parametrize('phrase', ['Tower BLK 160','Uber BLK 160','Dover PLK 160'])
def test_risky_stt_words_are_only_confirmed_contextual_suggestions(phrase):
    catalog = [row(1,'Dover','BLK','160'),row(2,'Dover','BLK','320')]
    assert products.resolve(catalog,phrase) == []
    assert products.contextual_suggestions(catalog,phrase) == []
    assert products.contextual_suggestions(catalog,phrase,context_product_ids=[2]) == []
    suggestion = products.contextual_suggestions(catalog,phrase,context_product_ids=[1])
    assert [item['id'] for item in suggestion] == [1]
    assert suggestion[0]['_voice_requires_confirmation']


def test_product_variant_display_retains_color_spacing_and_sku():
    label = products.display_name(row(1,'Dover','BB','160'))
    assert 'Dover BB 160' in label
    assert 'CAT-1-BB-160' in label


def test_voice_control_commands_do_not_become_products_or_generic_memory():
    assert parser.parse('kończymy na dziś').kind == 'pause'
    assert parser.parse('wznów liczenie').kind == 'resume'
    assert parser.parse('jak długo liczymy?').kind == 'elapsed'
    assert parser.parse('tę drugą').selection_index == 1
    assert parser.parse('Tak').decision == 'approve'
    assert parser.parse('nie').decision == 'reject'


def test_count_speech_never_asks_for_approval_before_it_is_prepared():
    import inventory_fast_voice
    answer = inventory_fast_voice.from_operation('inventory.count.record', {'difference':-2})
    assert 'Zatwierdzić' not in answer
    assert 'Korekta nie jest przygotowana' in answer
    assert 'Zatwierdzić' in inventory_fast_voice.from_operation(
        'inventory.count.record', {'difference':-2}, pending_adjustment=True)


def test_timing_never_invents_unknown_history():
    from inventory_fast_voice import timing_summary
    assert 'Brak zapisanej historii czasu' in timing_summary({'active_seconds':None})
    partial = timing_summary({'active_seconds':65,'calendar_seconds':180,'timing_complete':False})
    assert '1 min 5 s' in partial and '3 min 0 s' in partial
    assert 'wcześniejszej pracy' in partial

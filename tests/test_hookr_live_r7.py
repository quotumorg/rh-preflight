"""Round 7 project-side attribution. All transports here are synthetic."""
from copy import deepcopy
import re

import pytest

from rh_preflight import hookr_collect as collect, income, payer_attribution as core
from rh_preflight.card import token_card
from rh_preflight.hookr_live import ZERO
from hookr_synthetic import (
    TOKEN, OWNER, PAYER, OUTSIDER, DOOR, BOX, ASSET, POOL,
    reader, synthetic_model, funding_row, history_transport, synthetic_log,
)

ROOTS = {OWNER, TOKEN, DOOR, BOX}
STAMP = 1_800_000_000


def bounded(rows=(), missing=None, kind='txlist'):
    r = reader(depth=1)
    # Repeated outgoing swaps fill the sender's descending pages but are not
    # evidence of a hub. The single counterparty stays the same.
    busy = [funding_row(PAYER, OUTSIDER, block=350-i, stamp=STAMP-100+i,
                        tx=1000+i, kind=kind, asset=ASSET) for i in range(150)]
    calls = history_transport(r, [*busy, *rows], missing)
    rel = collect.relationships(r, PAYER, 400, 410, ROOTS, STAMP, STAMP+10, {400, 405, 410})
    collect.project_outgoing(r, ROOTS, [rel])
    return r, rel, calls


@pytest.mark.parametrize('kind', ['txlist', 'txlistinternal', 'tokentx'])
def test_truncated_sender_does_not_block_fully_read_project_side(kind):
    r, rel, calls = bounded(kind=kind)
    assert rel['fundingScope']['candidateHistoryComplete'] is False
    assert rel['relationshipsComplete'] is False
    assert rel['accounts'][PAYER]['hub'] is False
    assert core.relationship(PAYER, synthetic_model()['identity'], rel, True)['classification'] == core.EXTERNAL
    project_calls = {(q['address'], q['action']) for q in calls if q['action'] in ('txlist','txlistinternal','tokentx')}
    assert {(w,k) for w in ROOTS for k in ('txlist','txlistinternal','tokentx')} <= project_calls
    codes = {int(q['tag'],16) for q in calls if q['action']=='eth_getCode' and q['address']==PAYER}
    assert codes == {399,400,404,405,409,410}


@pytest.mark.parametrize('root', sorted(ROOTS))
@pytest.mark.parametrize('kind', ['txlist', 'txlistinternal', 'tokentx'])
def test_every_project_origin_and_history_kind_must_be_complete(root, kind):
    _, rel, _ = bounded(missing=(root,kind))
    assert not rel['fundingScope']['projectOutgoingComplete']
    assert core.relationship(PAYER, synthetic_model()['identity'], rel, True)['classification'] == core.UNKNOWN


@pytest.mark.parametrize('origin', sorted(ROOTS))
def test_project_side_finds_payment_hidden_below_sender_page_cap(origin):
    transfer = funding_row(origin,PAYER,block=50,stamp=STAMP-1000,tx=5)
    _, rel, _ = bounded([transfer])
    assert rel['fundingScope']['candidateHistoryComplete'] is False
    assert rel['fundingScope']['directProjectFunding'] is True
    assert any(p['path'][0]['from']==origin for p in rel['paths'])
    ev = core.relationship(PAYER,synthetic_model()['identity'],rel,True)
    assert ev['classification'] == (core.CREATOR if origin==OWNER else core.UNKNOWN)
    # Prior funding is a relationship, never a verified payment of LP income.
    model = synthetic_model()
    model['attributions'][0]['allocations'][0]['senderEvidence'] = ev
    facts = income.classify(TOKEN,model)
    assert facts['severity']=='INFO' and not any(s['severity']=='WARN' for s in facts['sources'])


@pytest.mark.parametrize('verified', [True, False])
def test_prior_project_origin_lead_survives_normalization_and_render(verified):
    model = synthetic_model()
    part = model['attributions'][0]['allocations'][0]
    ev = part['senderEvidence']
    before = ev['beforeBlock']
    lead = {'path':[{'from':TOKEN,'to':PAYER,'currency':ASSET,'amount':'10',
                    'blockNumber':str(before-1),'time':ev['beforeTime']-1,
                    'txHash':'0x'+'ab'*32,'nativeFundingVerified':verified}]}
    ev['fundingLeads'] = [lead]
    # Even a claimed-complete negative scan cannot erase a positive lead.
    facts = income.classify(TOKEN,model)
    assert all(s['classification']==core.UNKNOWN for s in facts['sources'])
    assert 'warn:' not in '\n'.join(facts['lines'])


@pytest.mark.parametrize('gap', ['contract','known-hub','identity-hub','missing-code','previous-code','middle-code',
                               'project-roots','old-rule','depth-zero','common-funder','literal-token'])
def test_external_evidence_gaps_remain_unknown(gap):
    model = synthetic_model()
    part = model['attributions'][0]['allocations'][0]
    ev = part['senderEvidence']; account = ev['accounts'][PAYER]
    block = ev['beforeBlock']
    if gap=='contract': account['code']='0x6000'
    if gap=='known-hub':
        payer=next(iter(core.KNOWN_HUBS)); part['sender']=payer; ev['sender']=payer
        ev['accounts'][payer]=account
    if gap=='identity-hub': model['identity']['hubs']=[PAYER]
    if gap=='missing-code': account.pop('codeChecks')
    if gap=='previous-code': account['codeChecks'][str(block-1)]='0x6000'
    if gap=='middle-code':
        part['weight']='1200'; part['totalWeight']='2000'
        extra=deepcopy(part['swaps'][0]); extra.update(txHash='0x'+'cc'*32,blockNumber=str(block+1))
        part['swaps'].append(extra)
        ev['fundingScope']['throughBlock']=block+2
        account['codeChecks'][str(block+2)]='0x'
        account['codeChecks'][str(block+1)]='0x6000'
    if gap=='project-roots': ev['fundingScope']['projectOutgoingRoots'].remove(TOKEN)
    if gap=='old-rule': ev['fundingScope']['rule']='direct-funding-window/v1'
    if gap=='depth-zero': ev['relationshipDepth']=0
    if gap=='common-funder': ev['note']='common funder lead'
    if gap=='literal-token':
        model['identity']['token']=PAYER
        # The pure rule also rejects literal non-creator project endpoints.
        rel={'depth':1,'hubRules':core.HUB_RULES,'beforeBlock':block,'beforeTime':ev['beforeTime'],
             'lookbackSeconds':ev['lookbackSeconds'],'accounts':ev['accounts'], 'fundingScope':ev['fundingScope']}
        assert core.relationship(PAYER,model['identity'],rel,True)['classification']==core.UNKNOWN
        return
    facts = income.classify(TOKEN,model)
    assert all(s['classification']==core.UNKNOWN for s in facts['sources'])


def test_offline_external_survives_truncated_sender_result_and_no_wallets():
    model=synthetic_model()
    model['live']['fundingLookbackDays']=7
    ev=model['attributions'][0]['allocations'][0]['senderEvidence']
    ev['relationshipsComplete']=False
    ev['fundingScope']['candidateHistoryComplete']=False
    facts=income.classify(TOKEN,model)
    assert facts['sources'][0]['classification']==core.EXTERNAL
    assert facts['live']['currencyShares'][0]['tracedAmount']=='60'
    for text in ('\n'.join(facts['lines']),token_card(TOKEN,{},hookr_income=model).render()):
        assert 'external payer (approximate)' in text and 'all of their outgoing transfers read' in text
        assert 'indirect funding' not in text and 'warn:' not in text
        assert set(re.findall(r'0x[0-9a-fA-F]{40}(?![0-9a-fA-F])',text)) <= {TOKEN}


def test_external_cannot_bypass_missing_yield_reconciliation():
    model=synthetic_model()
    model['attributions'][0]['allocations'][0]['reconciliationOk']=False
    facts=income.classify(TOKEN,model)
    assert facts['live']['currencyShares'][0]['tracedAmount']=='0'
    assert all(s['classification']==core.UNKNOWN for s in facts['sources'])


def test_selection_reports_basis_shortfall_and_cap_without_copying_pool_totals():
    r=reader(max_swap_txs=1)
    trades=[collect.decode_swap(synthetic_log(tx=i,units=w),POOL,[ZERO,ASSET]) for i,w in ((1,4),(2,3))]
    for t,v in zip(trades,(4,3)): t['traceValue']=v
    r.get_tx=lambda h:{'hash':h,'from':PAYER,'blockNumber':'0x14'}
    empty='0x'+'99'*32
    results=collect.resolve_senders(r,{POOL:{'trades':trades,'traceTotals':{ZERO:100}},
        empty:{'trades':[],'currencies':[ZERO,ASSET],'traceTotals':{ZERO:100}}},set())
    items={p['poolId']:p['currencies'][0] for p in results}
    assert items[POOL]['selectedWeight']=='4' and items[POOL]['availableWeight']=='7'
    assert items[empty]['selectedWeight']=='0'
    assert items[POOL]['shortfallReasons']==['income outside observed swap fee basis','swap sender transaction cap reached']
    model=synthetic_model(); model['live']['senderResolution']=results
    text='\n'.join(income.classify(TOKEN,model)['lines'])
    assert 'income outside observed swap fee basis; swap sender transaction cap reached' in text
    assert '0.00% income value selected' in text


@pytest.mark.parametrize('leads', [None,{},'bad'])
def test_malformed_private_leads_stay_unknown(leads):
    model=synthetic_model(); model['attributions'][0]['allocations'][0]['senderEvidence']['fundingLeads']=leads
    assert all(s['classification']==core.UNKNOWN for s in income.classify(TOKEN,model)['sources'])


@pytest.mark.parametrize('gap', [None,'project-token','preceding-code'])
def test_full_collection_truncated_sender_project_side_and_code_checks(monkeypatch,gap):
    model=synthetic_model(); raw=model['income']; row=raw['lines'][0]
    row.update(amount='100',blockNumber='200',time=2000)
    raw.update(status='ready',linesOmitted=0,notCounted=[])
    raw['window'].update(days=1,through=2000,throughBlock=200,recordStarts=0,cut=False)
    raw['window']['from']=1000
    r=reader(depth=1)
    busy=[funding_row(PAYER,OUTSIDER,block=50,stamp=1400,tx=1000+i) for i in range(150)]
    calls=history_transport(r,busy,missing=(TOKEN,'tokentx') if gap=='project-token' else None)
    if gap=='preceding-code':
        old=r.code
        r.code=lambda wallet,height=None: '0x6000' if wallet==PAYER and height==99 else old(wallet,height)
    meta={'status':'ready','token':{'address':TOKEN},'tokenizer':'v2',
          'tokenizerAddress':collect.TOKENIZER,'blockNumber':'200','creator':OWNER,
          'addingStreams':{'manager':OWNER},'drops':{'box':BOX},
          'streams':{'status':'ready','streams':[{'door':DOOR,'kind':'market'}]}}
    def mcp(name,args=None):
        if name=='hookr_revenue_token': return deepcopy(meta)
        if name=='hookr_income': return deepcopy(raw)
        if name=='tools/list': return {'tools':[{'name':'hookr_pool_yield','inputSchema':{'properties':{'poolId':{},'days':{}}}}]}
        if name=='hookr_pool_yield': return {'status':'ready','poolId':POOL,'window':{'fromBlock':1,'throughBlock':200},
            'swapFees':[{'currency':ZERO,'amount':'100'},{'currency':ASSET,'amount':'0'}]}
        raise AssertionError(name)
    r.mcp=mcp
    r.creation=lambda *a:{'deployer':OWNER,'deploymentSender':OWNER}
    r.get_delivery=lambda *a:{'txHash':row['txHash'],'blockNumber':'200','keeper':OUTSIDER,'doors':[DOOR],
        'pools':[{'poolId':POOL,'collect':True,'sender':collect.POOL_MANAGER,'logIndex':'10'}],
        'forwarding':[{'from':DOOR,'to':TOKEN,'currency':ZERO,'amount':'100'}]}
    trades=[{'txHash':'0x'+f'{b:064x}','blockNumber':str(b),'time':t,'currency':ZERO,'poolId':POOL,
             'amount':'5000','feePips':10000,'feeWeight':'50000000','feeAmount':'50','logIndex':1,'sender':None}
            for b,t in [(100,1500),(110,1600)]]
    r.get_tx=lambda h:{'hash':h,'blockNumber':hex(int(h,16)),'from':PAYER,'to':collect.POOL_MANAGER}
    monkeypatch.setattr(collect,'swap_scan',lambda *a:{'poolId':POOL,'trades':deepcopy(trades),'complete':True,
        'fromBlock':1,'throughBlock':200,'currencies':[ZERO,ASSET]})
    result=r.read(TOKEN,1); facts=income.classify(TOKEN,result)
    assert facts['sources'][0]['classification']==(core.EXTERNAL if gap is None else core.UNKNOWN)
    assert facts['severity']=='INFO'
    scope=result['live']['funding'][0]['fundingScope']
    assert set(scope['projectOutgoingRoots'])==ROOTS
    assert scope['projectOutgoingComplete'] is (gap!='project-token')
    if gap is None:
        assert not scope['candidateHistoryComplete']
        assert facts['live']['currencyShares'][0]['tracedAmount']=='100'


def test_project_lead_from_core_is_retained_for_offline_revalidation():
    _,rel,_=bounded()
    rel['paths']=[{'path':[{'from':TOKEN,'to':PAYER,'currency':ASSET,'amount':'1',
                           'blockNumber':'50','txHash':'0x'+'ee'*32,'time':STAMP-1000}]}]
    ev=core.relationship(PAYER,synthetic_model()['identity'],rel,True)
    assert ev['classification']==core.UNKNOWN and ev['fundingLeads']==rel['paths']
    model=synthetic_model()
    part=model['attributions'][0]['allocations'][0]
    # Keep the model's valid bounds and reuse only the normalized lead list.
    part['senderEvidence']['fundingLeads']=ev['fundingLeads']
    assert all(s['classification']==core.UNKNOWN for s in income.classify(TOKEN,model)['sources'])


def test_combined_currency_target_does_not_require_eighty_percent_from_each_pool():
    other='0x'+'a1'*32
    one=collect.decode_swap(synthetic_log(tx=1),POOL,[ZERO,ASSET]); one['traceValue']=40
    two={**one,'txHash':'0x'+'02'*32,'poolId':other}
    pages={POOL:{'trades':[one]},other:{'trades':[two]}}
    coverage=collect.selection_coverage(pages,{ZERO:100},{one['txHash'],two['txHash']},set(),8000)
    assert all(p['currencies'][0]['selectedWeight']=='40' for p in coverage)
    assert all(p['currencies'][0]['shortfallReasons']==[] for p in coverage)

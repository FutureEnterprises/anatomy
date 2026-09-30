"""The share card: numbers only, the user's top three fixes and the one evaluated trick that would have cost them money.

Built from a scan report that carries audits. The card holds numbers, fixed
labels and the price snapshot dates; the sentences come from a fixed vocabulary
in audits/__init__.py. Both renderings (text and SVG) pass the privacy gate
before they are returned, and the card says when the sample is below the
minimum group size for sharing (20 threads and 5 sessions). With shares_only
the card holds no dollar amounts at all: spend and every fix are shares of the
spend read, for posting a card without disclosing what you spend.
"""
from __future__ import annotations

from . import audits as A
from .privacy import assert_clean, gate

SVG_NS = 'http://www.w3.org/2000/svg'   # the only URL in the SVG; a constant, never user data
TEXT_PARAMS = ('cap_minutes_5m', 'cap_tokens', 'batch_tokens', 'vendors', 'breakeven_refetch_rate')   # numbers a fix sentence quotes


def _share(x, total):
    return round(x / total, 4) if total else None


def build(rep: dict, shares_only: bool = False) -> dict:
    au = rep['audits']
    c = rep.get('claude') or {}
    served = c.get('list_price_usd', {}).get('served', 0.0)
    declined = c.get('declined_fallbacks_usd', {}).get('declined_billed', 0.0)
    codex = (rep.get('codex') or {}).get('list_price_usd', {}).get('total', 0.0)
    spend = served + declined + codex
    fixes = []
    for f in A.top_fixes(au):
        if round(f['net_usd'], 2) <= 0:
            continue    # below a cent at display precision
        fixes.append({'basis': 'modeled', 'id': f['id'], 'audit': f['audit'], 'net_usd': round(f['net_usd'], 2),
                      'share_of_spend': _share(f['net_usd'], spend),
                      **{k: f[k] for k in TEXT_PARAMS if k in f}})
    t = A.costly_trick(au)
    trick = None
    if t:
        trick = {'basis': 'modeled', 'id': t['id'], 'net_usd': round(t['net_usd'], 2),
                 'share_of_spend': _share(t['net_usd'], spend),
                 'zero_refetch': bool(t.get('zero_refetch'))}
    ev = {'basis': 'modeled', 'refetch_rate': 0.0}
    for v in ('claude', 'codex'):
        sec = au['breakeven'].get(v + '_eviction_opportunities')
        if sec and sec['batches']:
            ev[v + '_batches'] = sec['batches']
            ev[v + '_share_that_pay'] = sec['share_batches_that_pay']
    card = {
        'card_version': 'anatomy-card-1',
        'unit': A.LIST_PRICE,
        'prices': {'anthropic_snapshot': rep['prices']['anthropic']['snapshot_date'],
                   'openai_snapshot': rep['prices']['openai']['snapshot_date']},
        'spend': {'basis': 'observed', 'claude_served_usd': round(served, 2), 'codex_usd': round(codex, 2)},
        'spend_declined': {'basis': 'estimated', 'claude_declined_fallbacks_usd': round(declined, 2), 'billing_assumed': True},
        'fixes': fixes,
        'costly_trick': trick,
        'eviction_payback': ev,
        'sample': dict(au['sample']),
    }
    if shares_only:
        card['shares_only'] = True
        card['spend'] = {'basis': 'observed', 'claude_served_share': _share(served, spend), 'codex_share': _share(codex, spend)}
        card['spend_declined'] = {'basis': 'estimated', 'claude_declined_fallbacks_share': _share(declined, spend),
                                  'billing_assumed': True}
        for f in fixes:
            del f['net_usd']
        if trick:
            del trick['net_usd']
    gate(card)
    return card


def _money(x):
    return ('-$' if x < 0 else '$') + '{:,.0f}'.format(abs(x))


def _pct(x):
    return '{:.1f}%'.format(100 * x) if x is not None else 'n/a'


def lines(card: dict) -> list:
    """(kind, text) rows shared by the text and SVG renderings. kind: title, meta, head, row, note."""
    so = card.get('shares_only', False)
    prices = 'Prices: Anthropic %s, OpenAI %s.' % (card['prices']['anthropic_snapshot'], card['prices']['openai_snapshot'])
    if so:
        sp, dc = card['spend'], card['spend_declined']['claude_declined_fallbacks_share']
        R = [('title', 'ANATOMY CARD'),
             ('meta', 'Shares of spend read, priced at %s, not an invoice. Dollar amounts hidden. %s' % (card['unit'], prices)),
             ('row', 'Spend read: Claude Code %s served, Codex %s  [observed]' % (
                 _pct(sp['claude_served_share']), _pct(sp['codex_share'])))]
        if dc:
            R.append(('note', 'Plus %s of declined Claude Code fallback attempts, billing assumed, included in spend read  [estimated]'
                      % _pct(dc)))
    else:
        dc = card['spend_declined']['claude_declined_fallbacks_usd']
        R = [('title', 'ANATOMY CARD'),
             ('meta', 'Dollars are %s, not an invoice. %s' % (card['unit'], prices)),
             ('row', 'Spend read: Claude Code %s served, Codex %s  [observed]' % (
                 _money(card['spend']['claude_served_usd']), _money(card['spend']['codex_usd'])))]
        if dc:
            R.append(('note', 'Plus %s of declined Claude Code fallback attempts, billing assumed, included in spend read  [estimated]'
                      % _money(dc)))
    R.append(('head', 'TOP FIXES THAT CLEAR BREAK-EVEN AT YOUR PRICES'))
    if not card['fixes']:
        R.append(('row', 'None of the audited fixes clears break-even at your prices.'))
    for i, f in enumerate(card['fixes'], 1):
        R.append(('row', '%d. %s  [modeled]' % (i, A.fix_text(f))))
        if so:
            R.append(('note', '   would have saved %s of spend read in replay  [modeled]' % _pct(f['share_of_spend'])))
        else:
            R.append(('note', '   would have saved %s in replay, %s of spend read  [modeled]' % (_money(f['net_usd']), _pct(f['share_of_spend']))))
    R.append(('head', 'THE EVALUATED TRICK THAT WOULD HAVE COST YOU MONEY'))
    t = card['costly_trick']
    if t:
        R.append(('row', A.trick_text(t) + '  [modeled]'))
        share = _pct(-t['share_of_spend']) if t['share_of_spend'] is not None else 'n/a'
        tail = ', even with no re-fetches' if t['zero_refetch'] else ''
        if so:
            R.append(('note', '   would have cost %s of spend read in replay%s  [modeled]' % (share, tail)))
        else:
            R.append(('note', '   would have cost %s in replay, %s of spend read%s  [modeled]' % (_money(-t['net_usd']), share, tail)))
    else:
        R.append(('row', 'None of the evaluated tricks loses money at your prices.'))
    ev = card['eviction_payback']
    parts = ['%s %s of %s' % (name, _pct(ev[v + '_share_that_pay']), '{:,}'.format(ev[v + '_batches']))
             for v, name in (('claude', 'Claude Code'), ('codex', 'Codex')) if v + '_batches' in ev]
    if parts:
        R.append(('note', 'Eviction opportunities that pay back at your prices, before re-fetches: %s  [modeled]' % ', '.join(parts)))
    s = card['sample']
    R.append(('note', 'Sample: %s threads, %s sessions. %s (%d threads and %d sessions).  [observed]' % (
        '{:,}'.format(s['threads']), '{:,}'.format(s['sessions']),
        'Meets the sharing minimum' if s['meets_sharing_minimum'] else 'Below the sharing minimum, do not share',
        s['minimum_threads'], s['minimum_sessions'])))
    return R


def render_text(card: dict) -> str:
    out = '\n'.join(t for _, t in lines(card)) + '\n'
    return assert_clean(out)


def _esc(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace("'", '&#39;').replace('"', '&quot;')


def _wrap(s: str, width: int) -> list:
    words, out, cur = s.split(' '), [], ''
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = w
        else:
            cur = (cur + ' ' + w) if cur else w
    if cur:
        out.append(cur)
    return out


def render_svg(card: dict) -> str:
    """A 1200-wide card: solid paper background, black type, one accent color for the numbers."""
    ink, paper, accent, muted = '#111111', '#F4F1EA', '#C8102E', '#5A5A5A'
    y = 72
    body = []
    for kind, text in lines(card):
        if kind == 'title':
            body.append('<text x="64" y="%d" font-size="44" font-weight="800" letter-spacing="3" fill="%s">%s</text>' % (y, ink, _esc(text)))
            y += 40
            continue
        size, weight, color, width = {'meta': (16, 400, muted, 140), 'head': (18, 800, accent, 90),
                                      'row': (22, 600, ink, 88), 'note': (18, 400, muted, 116)}[kind]
        if kind == 'head':
            y += 22
        for part in _wrap(text.strip(), width):
            y += int(size * 1.45)
            body.append('<text x="64" y="%d" font-size="%d" font-weight="%d" fill="%s">%s</text>' % (y, size, weight, color, _esc(part)))
    h = y + 56
    svg = ('<svg xmlns="%s" width="1200" height="%d" viewBox="0 0 1200 %d" '
           'font-family="Helvetica Neue, Helvetica, Arial, sans-serif">'
           '<rect width="1200" height="%d" fill="%s"/>'
           '<rect x="64" y="%d" width="120" height="6" fill="%s"/>%s</svg>\n') % (
        SVG_NS, h, h, h, paper, h - 36, accent, ''.join(body))
    assert_clean(svg.replace(SVG_NS, ''))
    return svg

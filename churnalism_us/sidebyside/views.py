# -*- coding: utf-8 -*-

from __future__ import division

import re
import lxml.html
import readability
from operator import itemgetter

import stream
from lepl.apps.rfc3696 import HttpUrl
from django.shortcuts import render
from django.http import Http404
from django.views.decorators.csrf import csrf_exempt
from apiproxy.embellishments import embellish
from utils.slurp_url import slurp_url

import superfastmatch

from django.conf import settings


def ensure_url(url):
    if url.startswith('http://') or url.startswith('https://'):
        return url
    
    url = 'http://' + url

    validator = HttpUrl()
    if validator(url) == True:
        return url
    
    raise ValueError('{url} is not a valid URL.'.format(url=url))


def render_text(el):
    """ like lxml.html text_content(), but with tactical use of whitespace for block elements """

    inline_tags = ( 'a', 'abbr', 'acronym', 'b', 'basefont', 'bdo', 'big',
        'br',
        'cite', 'code', 'dfn', 'em', 'font', 'i', 'img', 'input',
        'kbd', 'label', 'q', 's', 'samp', 'select', 'small', 'span',
        'strike', 'strong', 'sub', 'sup', 'textarea', 'tt', 'u', 'var',
        'applet', 'button', 'del', 'iframe', 'ins', 'map', 'object',
        'script' )

    txt = u''
    if isinstance(el, lxml.html.HtmlComment):
        return txt

    tag = str(el.tag).lower()
    if tag not in inline_tags:
        txt += u"\n";

    if el.text is not None:
        txt += unicode(el.text)
    for child in el.iterchildren():
        txt += render_text(child)
        if child.tail is not None:
            txt += unicode(child.tail)

    if el.tag=='br' or tag not in inline_tags:
        txt += u"\n";

    txt = re.sub(r'[\r\n]{2,}', '\n\n', txt)
    txt = re.sub(r'[\r\n]\s+[\r\n]', '\n\n', txt)

    return txt


def attach_document_text(results, maxdocs=None):
    sfm = superfastmatch.DjangoClient('sidebyside')
    if maxdocs:
        results['documents']['rows'].sort(key=itemgetter('characters'))

    for (idx, row) in enumerate(results['documents']['rows']):
        if maxdocs and idx >= maxdocs:
            return

        doc_result = sfm.document(row['doctype'], row['docid'])
        if doc_result['success'] == True:
            row['text'] = doc_result['text']


def search_page(request):
    return render(request, 'search_page.html', {})


def search_result_page(request, results, source_text, 
                       source_url=None, source_title=None):
    embellish(source_text, 
              results, 
              reduce_frags=True,
              add_coverage=True, 
              add_snippets=True,
              prefetch_documents=settings.SIDEBYSIDE.get('max_doc_prefetch'))
    return render(request, 'search_result.html',
                  {'results': results,
                   'source_text': source_text,
                   'source_title': source_title,
                   'source_url': source_url})


def search(request, url=None, uuid=None):
    if request.method == 'GET':
        url = url or request.GET.get('url')
        if url not in ('', None):
            return search_against_url(request, url)

        uuid = uuid or request.GET.get('uuid')
        if uuid not in ('', None):
            return search_against_uuid(request, uuid)

    elif request.method == 'POST':
        url = request.POST.get('url')
        if url not in ('', None):
            return search_against_url(request, url)

        text = request.POST.get('text')
        if text not in ('', None):
            return search_against_text(request, text)

    raise Http404()


def search_against_text(request, text):
    sfm = superfastmatch.DjangoClient('sidebyside')
    sfm_results = sfm.search(text)
    return search_result_page(request, sfm_results, text)


def search_against_uuid(request, uuid):
    sfm = superfastmatch.DjangoClient('sidebyside')
    sfm_results = sfm.search(text=None, uuid=uuid)
    return search_result_page(request, sfm_results, 
                              source_text=sfm_results.get('text'), 
                              source_title=sfm_results.get('title'))


def search_against_url(request, url):
    """
    Accepts a URL as either a suffix of the URI or a POST request 
    parameter. Downloads the content, feeds it through the
    readability article grabber, then submits the article text
    to superfastmatch for comparison.
    """

    def fetch_and_clean(url):
        html = slurp_url(url)
        if not html:
            raise Exception('Failed to fetch {0}'.format(url))

        doc = readability.Document(html)
        cleaned_html = doc.summary()
        content_dom = lxml.html.fromstring(cleaned_html)
        title = doc.short_title()
        return (title, render_text(content_dom).strip().encode('utf-8', 'replace').decode('utf-8'))

    sfm = superfastmatch.DjangoClient('sidebyside')
    (title, text) = fetch_and_clean(url)
    sfm_results = sfm.search(text=text, title=title, url=url)

    return search_result_page(request, sfm_results, sfm_results['text'],
                              source_title=sfm_results['title'], source_url=url)


def permalink(request, uuid, doctype, docid):
    sfm = superfastmatch.DjangoClient('sidebyside')
    sfm_results = sfm.search(text=None, uuid=uuid)

    for row in sfm_results['documents']['rows']:
        if row['doctype'] == doctype and row['docid'] == docid:
            if not row.get('text'):
                doc = sfm.document(doctype, docid)
                if doc:
                    row['text'] = doc['text']

    return search_result_page(request, sfm_results,
                              source_text=sfm_results['text'],
                              source_title=sfm_results.get('title'),
                              source_url=sfm_results.get('url'))


def select_best_match(text, sfm_results):
    rows = sfm_results['documents']['rows']

    def fragment_match_percentage(row):
        chars_matched = sum((frag[2] for frag in row['fragments']))
        pct_of_match = float(chars_matched) / float(row['characters'])
        pct_of_source = float(chars_matched) / float(len(text))
        return (row, max(pct_of_match, pct_of_source))

    def longer(a, b):
        (row_a, pct_a) = a
        (row_b, pct_b) = b
        return a if pct_a >= pct_b else b

    return (stream.Stream(rows)
            >> stream.map(fragment_match_percentage)
            >> stream.reduce(longer, (None, 0)))



@csrf_exempt
def chromeext_search(request):
    text = request.POST.get('text') or request.GET.get('text') or ''
    url = request.POST.get('url') or request.GET.get('url') or ''
    title = request.POST.get('title') or request.GET.get('title') or ''

    sfm = superfastmatch.DjangoClient('sidebyside')
    sfm_results = sfm.search(text=text, url=url)
    if url and not text:
        text = sfm_results['text']
    if url and not title:
        title = sfm_results['title']

    if not text:
        text = sfm_results['text']

    (match, match_pct) = select_best_match(text, sfm_results)
    if match is not None:
        match_doc = sfm.document(match['doctype'], match['docid'])
        if match_doc['success'] == True:
            match_text = match_doc['text']
            match_title = match.get('title', '')
            match_url = match.get('url', '')
        sfm_results['documents']['rows'] = [match]
        embellish(text, sfm_results, add_snippets=True)
    else:
        match_text = ''
        match_title = ''
        match_url = ''
        
    resp = render(request, 'chrome.html',
                  {'ABSOLUTE_STATIC_URL': request.build_absolute_uri(settings.STATIC_URL),
                   'ABSOLUTE_BASE_URL': request.build_absolute_uri('/'),
                   'results': sfm_results,
                   'source_text': text,
                   'source_title': title,
                   'source_url': url,
                   'uuid': sfm_results['uuid'],
                   'match': match,
                   'match_text': match_text,
                   'match_title': match_title,
                   'match_url': match_url})
    return resp

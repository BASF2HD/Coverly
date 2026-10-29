from pathlib import Path
from coverly.extraction import adapter_for, image_key, GenericAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def test_gallery_and_high_resolution():
    html = (FIXTURES / "product.html").read_text(encoding='utf-8')
    result = adapter_for(html).extract(html, "https://records.example/product/nayakan/")
    assert result["metadata"]["title"] == "Nayakan"
    assert result["metadata"]["artist"] == "Ilaiyaraaja"
    assert result["metadata"]["year"] == "1987"
    assert len(result["images"]) == 3
    assert result["images"][0]["url"].endswith("/front.jpg")
    assert all("sponsor" not in x["url"] for x in result["images"])
    assert len(result["images"][0]["alternatives"]) == 2


def test_category_does_not_merge_products():
    html = (FIXTURES / "category.html").read_text(encoding='utf-8')
    result = adapter_for(html).extract(html, "https://records.example/tamil/")
    assert not result["is_product"]
    assert result["images"] == []
    assert any(x["pagination"] for x in result["links"])


def test_microdata_sibling_images():
    result = GenericAdapter().extract('<main itemscope itemtype="https://schema.org/Product"><h1>Test LP</h1><img itemprop="image" src="/a.jpg"><img itemprop="image" src="/b.jpg"></main>', "https://records.example/product/test/")
    assert len(result["images"]) == 2


def test_query_image_identity():
    assert image_key("https://example.com/image?file=front.jpg") != image_key("https://example.com/image?file=back.jpg")
    assert image_key("https://example.com/front-150x150.jpg") == image_key("https://example.com/front.jpg")
    assert image_key("https://example.com/image?file=a&w=150") == image_key("https://example.com/image?file=a&w=1200")


def test_filtered_tamil_category_preserves_pagination_filter():
    url='https://records.example/product-category/vinyl-records/?filter_language=tamil'
    html='''<main class="woocommerce"><ul class="products"><li class="product"><a href="/product/album/">Album LP</a></li></ul>
    <a rel="next" href="/product-category/vinyl-records/page/2/">Next</a>
    <a class="page-numbers" href="/product-category/vinyl-records/page/3/?filter_language=hindi">Hindi</a></main>'''
    result=adapter_for(html).extract(html,url)
    links=[link['url'] for link in result['links']]
    assert 'https://records.example/product/album/' in links
    assert 'https://records.example/product-category/vinyl-records/page/2/?filter_language=tamil' in links
    assert not any('filter_language=hindi' in link for link in links)


def test_lazy_gallery_and_cdn_alternatives():
    html = '<div class="product-detail"><h1>Test</h1><img alt="front" src="data:image/png;base64,test" data-src="/small.jpg" data-original="https://cdn.example.com/original.jpg" data-srcset="/small.jpg 200w, /large.jpg 1500w"></div>'
    result = GenericAdapter().extract(html, "https://example.com/product/x")
    assert len(result["images"]) == 1
    assert len(result["images"][0]["alternatives"]) == 3


def test_product_heading_avoids_seo_site_suffix_and_fake_compilation():
    from coverly.intelligence import identify
    html='''<main class="woocommerce"><h1 class="product_title">Panneer Pushpangal Tamil EP Vinyl Records By Ilaiyaraaja</h1><div class="woocommerce-product-gallery"><img src="/front.jpg"></div><script type="application/ld+json">{"@type":"Product","name":"Panneer Pushpangal | Buy Online – Records, Cassettes, DVDs & CDs"}</script></main>'''
    result=adapter_for(html).extract(html,'https://records.example/panneer-pushpangal/')
    assert 'Buy Online' not in result['metadata']['title']
    assert not identify(result['metadata'])['compilation']

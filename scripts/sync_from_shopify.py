"""Sync Livostyle Shopify catalog → JSON + Parquet + thumbnails.

Runs locally OR in GitHub Actions (uses env vars for Shopify auth).
Outputs:
  data/products.json       — full structured catalog
  data/products.parquet    — same data, columnar (for ML)
  data/collections.json    — collection groupings
  data/stats.json          — catalog stats (counts, last_synced)
  thumbnails/{handle}.jpg  — 200x200 preview per product
"""
import os, json, time, urllib.request, urllib.error
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Config ──────────────────────────────────────────────────────────────
SHOP = os.environ['SHOPIFY_SHOP_DOMAIN']
TOK  = os.environ['SHOPIFY_ACCESS_TOKEN']
VER  = os.environ.get('SHOPIFY_API_VERSION', '2025-01')

OUT_DIR = os.environ.get('OUT_DIR', '.')
DATA_DIR = f'{OUT_DIR}/data'
THUMB_DIR = f'{OUT_DIR}/thumbnails'

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

# ─── GraphQL ─────────────────────────────────────────────────────────────
def gql(q, v=None, retries=3):
    for attempt in range(retries):
        try:
            body = json.dumps({'query': q, 'variables': v or {}}).encode()
            req = urllib.request.Request(
                f'https://{SHOP}/admin/api/{VER}/graphql.json',
                data=body, method='POST',
                headers={'X-Shopify-Access-Token': TOK,
                         'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == retries - 1: raise
            time.sleep(2 ** attempt)

# ─── Catalog enumeration ─────────────────────────────────────────────────
PRODUCTS_Q = '''
query($c:String){
  products(first:50, after:$c, query:"status:active"){
    pageInfo{ hasNextPage endCursor }
    edges{ node{
      id handle title productType vendor tags
      descriptionHtml description
      onlineStoreUrl createdAt updatedAt publishedAt
      productCategory{ productTaxonomyNode{ id name fullName } }
      priceRangeV2{ minVariantPrice{ amount currencyCode }
                    maxVariantPrice{ amount currencyCode } }
      featuredImage{ url altText width height }
      images(first:8){ edges{ node{ url altText width height } } }
      variants(first:25){ edges{ node{
        id sku title price compareAtPrice
        availableForSale inventoryQuantity
        selectedOptions{ name value }
      } } }
      metafields(first:30, namespace:"custom"){ edges{ node{
        namespace key value type
      } } }
      reviewsAgg: metafield(namespace:"reviews", key:"rating"){ value }
      reviewsCnt: metafield(namespace:"reviews", key:"rating_count"){ value }
    } }
  }
}'''

def fetch_all_products():
    cursor = None
    out = []
    page = 0
    while True:
        page += 1
        r = gql(PRODUCTS_Q, {'c': cursor})
        d = r['data']['products']
        for e in d['edges']:
            out.append(e['node'])
        print(f'  page {page}: total {len(out)}', flush=True)
        if not d['pageInfo']['hasNextPage']: break
        cursor = d['pageInfo']['endCursor']
        time.sleep(0.15)
    return out

COLLECTIONS_Q = '''
query($c:String){
  collections(first:50, after:$c){
    pageInfo{ hasNextPage endCursor }
    edges{ node{
      id handle title descriptionHtml updatedAt
      productsCount{ count }
      image{ url altText }
      seo{ title description }
    } }
  }
}'''

def fetch_all_collections():
    cursor = None
    out = []
    while True:
        r = gql(COLLECTIONS_Q, {'c': cursor})
        d = r['data']['collections']
        for e in d['edges']:
            out.append(e['node'])
        if not d['pageInfo']['hasNextPage']: break
        cursor = d['pageInfo']['endCursor']
    return out

# ─── Normalize for open data ─────────────────────────────────────────────
def _parse_rating(r):
    """Shopify reviews.rating is a 'rating' type returning JSON {scale_min,scale_max,value}."""
    if not r or not r.get('value'): return None
    v = r['value']
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            return float(json.loads(v).get('value'))
        except Exception:
            return None

def normalize_product(p):
    """Strip Shopify GIDs, flatten edges, derive useful fields."""
    pid_short = p['id'].split('/')[-1]
    cat = (p.get('productCategory') or {}).get('productTaxonomyNode') or {}

    images = [n['node'] for n in (p.get('images') or {}).get('edges', [])]
    variants = []
    for ve in (p.get('variants') or {}).get('edges', []):
        v = ve['node']
        variants.append({
            'sku': v.get('sku'),
            'title': v.get('title'),
            'price_usd': float(v['price']) if v.get('price') else None,
            'compare_at_price_usd': float(v['compareAtPrice']) if v.get('compareAtPrice') else None,
            'in_stock': v.get('availableForSale'),
            'options': {o['name']: o['value'] for o in (v.get('selectedOptions') or [])},
        })

    metafields = {}
    for me in (p.get('metafields') or {}).get('edges', []):
        m = me['node']
        metafields[m['key']] = m['value']

    pmin = p.get('priceRangeV2', {}).get('minVariantPrice', {})
    pmax = p.get('priceRangeV2', {}).get('maxVariantPrice', {})

    return {
        'id': pid_short,
        'handle': p.get('handle'),
        'title': p.get('title'),
        'url': f"https://livostyle.com/products/{p.get('handle')}",
        'product_type': p.get('productType'),
        'vendor': p.get('vendor'),
        'tags': p.get('tags') or [],
        'description': p.get('description') or '',
        'description_html': p.get('descriptionHtml') or '',
        'category': {
            'id': cat.get('id', '').split('/')[-1] if cat else None,
            'name': cat.get('name'),
            'full_path': cat.get('fullName'),
        } if cat else None,
        'price': {
            'min_usd': float(pmin['amount']) if pmin.get('amount') else None,
            'max_usd': float(pmax['amount']) if pmax.get('amount') else None,
            'currency': pmin.get('currencyCode', 'USD'),
        },
        'images': [{
            'url': i['url'],
            'alt': i.get('altText'),
            'width': i.get('width'),
            'height': i.get('height'),
        } for i in images],
        'featured_image_url': (p.get('featuredImage') or {}).get('url'),
        'variants': variants,
        'metafields': metafields,
        'reviews': {
            'rating': _parse_rating(p.get('reviewsAgg')),
            'count': int(p['reviewsCnt']['value']) if p.get('reviewsCnt') else None,
        },
        'created_at': p.get('createdAt'),
        'updated_at': p.get('updatedAt'),
    }

def normalize_collection(c):
    return {
        'id': c['id'].split('/')[-1],
        'handle': c.get('handle'),
        'title': c.get('title'),
        'url': f"https://livostyle.com/collections/{c.get('handle')}",
        'description_html': c.get('descriptionHtml') or '',
        'products_count': (c.get('productsCount') or {}).get('count', 0),
        'image_url': (c.get('image') or {}).get('url'),
        'seo': c.get('seo') or {},
        'updated_at': c.get('updatedAt'),
    }

# ─── Thumbnails (200x200, via Shopify CDN resizer) ────────────────────────
def download_thumb(handle, url):
    if not url: return False
    # Shopify CDN supports ?width=200 query for built-in resize
    resized = url.split('?')[0] + '?width=200&height=200&crop=center'
    out_path = f'{THUMB_DIR}/{handle}.jpg'
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
        return True  # already have it
    try:
        req = urllib.request.Request(resized, headers={'User-Agent': 'Livostyle-OpenData/1.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        with open(out_path, 'wb') as f:
            f.write(data)
        return True
    except Exception:
        return False

def download_thumbs_parallel(items, max_workers=8):
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(download_thumb, p['handle'], p['featured_image_url']): p['handle']
                for p in items if p.get('featured_image_url')}
        for i, f in enumerate(as_completed(futs)):
            if f.result(): ok += 1
            else: fail += 1
            if (ok + fail) % 100 == 0:
                print(f'    thumbs: {ok} ok, {fail} fail', flush=True)
    return ok, fail

# ─── Main ────────────────────────────────────────────────────────────────
def main():
    t0 = time.time()
    print('═══ Livostyle Open Data sync ═══', flush=True)

    print('\n[1/4] Fetching products…', flush=True)
    raw_products = fetch_all_products()
    products = [normalize_product(p) for p in raw_products]
    print(f'  ✓ {len(products)} products normalized')

    print('\n[2/4] Fetching collections…', flush=True)
    raw_cols = fetch_all_collections()
    collections = [normalize_collection(c) for c in raw_cols]
    print(f'  ✓ {len(collections)} collections normalized')

    # ─── JSON ────
    print('\n[3/4] Writing JSON…', flush=True)
    with open(f'{DATA_DIR}/products.json', 'w') as f:
        json.dump(products, f, indent=2, ensure_ascii=False)
    with open(f'{DATA_DIR}/collections.json', 'w') as f:
        json.dump(collections, f, indent=2, ensure_ascii=False)

    # ─── Stats ────
    cats = {}
    for p in products:
        c = (p.get('category') or {}).get('full_path', 'Uncategorized') if p.get('category') else 'Uncategorized'
        cats[c] = cats.get(c, 0) + 1
    stats = {
        'last_synced': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'total_products': len(products),
        'total_collections': len(collections),
        'products_with_reviews': sum(1 for p in products if (p.get('reviews') or {}).get('count')),
        'avg_review_count': round(sum((p.get('reviews') or {}).get('count') or 0 for p in products) / max(len(products), 1), 1),
        'avg_rating': round(sum((p.get('reviews') or {}).get('rating') or 0 for p in products) / max(sum(1 for p in products if (p.get('reviews') or {}).get('rating')), 1), 2),
        'products_by_category': dict(sorted(cats.items(), key=lambda x: -x[1])),
        'currency': 'USD',
        'source': 'https://livostyle.com',
        'license': 'MIT',
    }
    with open(f'{DATA_DIR}/stats.json', 'w') as f:
        json.dump(stats, f, indent=2)

    # ─── Parquet (optional, if pandas+pyarrow installed) ────
    try:
        import pandas as pd
        flat = []
        for p in products:
            flat.append({
                'id': p['id'],
                'handle': p['handle'],
                'title': p['title'],
                'url': p['url'],
                'product_type': p['product_type'],
                'category_full_path': (p.get('category') or {}).get('full_path'),
                'tags': ' | '.join(p.get('tags') or []),
                'description': p.get('description', '')[:2000],
                'price_min_usd': (p.get('price') or {}).get('min_usd'),
                'price_max_usd': (p.get('price') or {}).get('max_usd'),
                'in_stock_any': any(v.get('in_stock') for v in p.get('variants') or []),
                'variant_count': len(p.get('variants') or []),
                'image_count': len(p.get('images') or []),
                'featured_image_url': p.get('featured_image_url'),
                'review_count': (p.get('reviews') or {}).get('count'),
                'review_rating': (p.get('reviews') or {}).get('rating'),
                'created_at': p.get('created_at'),
                'updated_at': p.get('updated_at'),
            })
        df = pd.DataFrame(flat)
        df.to_parquet(f'{DATA_DIR}/products.parquet', engine='pyarrow', compression='snappy')
        print(f'  ✓ products.parquet ({len(df)} rows)')
    except ImportError:
        print('  (skipped parquet — install pandas+pyarrow to enable)')

    # ─── Thumbnails ────
    print('\n[4/4] Downloading 200x200 thumbnails…', flush=True)
    ok, fail = download_thumbs_parallel(products, max_workers=8)
    print(f'  ✓ {ok} thumbs ok, {fail} fail')

    print(f'\n═══ Done in {int(time.time()-t0)}s ═══')
    print(f'   products: {len(products)}')
    print(f'   collections: {len(collections)}')
    print(f'   thumbnails: {ok}')

if __name__ == '__main__':
    main()

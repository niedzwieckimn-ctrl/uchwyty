"""Bounded reads and ADJUST batches. No products, prices or invoices are written."""
import requests
from orderchamp_client import API_URL, OrderchampClient, OrderchampError

ORDER_FIELDS = """ id number updatedAt createdAt companyName companyPhone email vatNumber currency
 isCancelled isConfirmed isFulfilled isTest source subtotalPrice taxPrice totalPrice
 billingAddress { companyName name street houseNumber postalCode city country }
 shippingAddress { companyName name street houseNumber postalCode city country }
"""
ITEM_FIELDS = "id sku quantity unshippedQuantity unitPrice subtotalPrice taxPrice totalPrice"
ORDERS_DELTA = """query OrdersDelta($after:String,$since:DateTime) {
 orders(first:10,after:$after,updatedSince:$since,includeUnconfirmed:true,includeCancelled:true,sort:ID_ASC) {
  nodes { """ + ORDER_FIELDS + """
   products(first:50) { nodes { """ + ITEM_FIELDS + """ } pageInfo {hasNextPage endCursor} }
  } pageInfo {hasNextPage endCursor}
 }
}"""
ORDER_LINES = """query OrderLines($id:ID!,$after:String) {
 order(id:$id) { id updatedAt products(first:100,after:$after) {
  nodes { """ + ITEM_FIELDS + """ } pageInfo {hasNextPage endCursor}
 }}
}"""
VARIANTS = """query InventoryCatalog($after:String,$skus:[String!]) {
 productVariants(first:50,after:$after,skus:$skus) {
  nodes {id sku inventoryPolicy inventoryQuantity inventoryLevels(first:2) {
   nodes {id quantity availableQuantity updatedAt location {id isPrimary}}
   pageInfo {hasNextPage}
  }} pageInfo {hasNextPage endCursor}
 }
}"""
ADJUST = """mutation AdjustInventoryBatch($input:InventoryLevelBulkAdjustInput!) {
 inventoryLevelBulkAdjust(input:$input) {
  clientMutationId userErrors {field message}
  inventoryLevels {id quantity availableQuantity updatedAt}
 }
}"""


def connection_page(value, limit):
    if not isinstance(value, dict) or not isinstance(value.get('nodes'), list) or len(value['nodes'])>limit:
        raise OrderchampError('INVALID_API_RESPONSE')
    page = value.get('pageInfo')
    if not isinstance(page, dict) or type(page.get('hasNextPage')) is not bool:
        raise OrderchampError('INVALID_API_RESPONSE')
    cursor = page.get('endCursor') if page['hasNextPage'] else None
    if page['hasNextPage'] and (not isinstance(cursor, str) or not cursor):
        raise OrderchampError('INVALID_API_RESPONSE')
    return value['nodes'], cursor


class IncrementalClient(OrderchampClient):
    read_queries = (ORDERS_DELTA, ORDER_LINES, VARIANTS)

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.wait_seconds=0
        original_sleep=self._sleep
        def timed_sleep(seconds):
            self.wait_seconds+=seconds
            return original_sleep(seconds)
        self._sleep=timed_sleep

    def orders_page(self, since=None, after=None):
        orders, cursor = connection_page(self._read(ORDERS_DELTA, {'since':since,'after':after}).get('orders'),10)
        for order in orders:
            if not isinstance(order, dict):
                raise OrderchampError('INVALID_API_RESPONSE')
            lines, next_page = connection_page(order.get('products'),50)
            lines = list(lines)
            seen = set()
            while next_page:
                if next_page in seen or len(seen)>=20:
                    raise OrderchampError('ORDER_TOO_LARGE')
                seen.add(next_page)
                detail = self._read(ORDER_LINES, {'id':order['id'],'after':next_page}).get('order')
                if not detail or detail.get('id')!=order['id'] or detail.get('updatedAt')!=order['updatedAt']:
                    raise OrderchampError('ORDER_CHANGED_DURING_READ')
                extra, next_page = connection_page(detail.get('products'),100)
                lines.extend(extra)
            order['lines'] = lines
        return orders, cursor

    def variants_page(self, after=None,skus=None):
        return connection_page(self._read(VARIANTS, {'after':after,'skus':skus}).get('productVariants'),50)

    def adjust_batch(self, rows, mutation_id):
        if not 1<=len(rows)<=20 or len({r['level_id'] for r in rows})!=len(rows):
            raise OrderchampError('INVALID_ADJUSTMENT')
        for row in rows:
            if not isinstance(row['level_id'],str) or not row['level_id'] or type(row['delta']) is not int or not -2147483647<=row['delta']<=2147483647:
                raise OrderchampError('INVALID_ADJUSTMENT')
        self._sleep(max(0,self._next_request-self._monotonic()))
        self._next_request=self._monotonic()+0.5
        self.request_count+=1
        response=None
        try:
            response=self._session.post(API_URL,headers={'Authorization':'Bearer '+self._token,'Accept':'application/json'},
                json={'query':ADJUST,'variables':{'input':{'clientMutationId':mutation_id,
                  'inventoryLevels':[{'inventoryLevelId':r['level_id'],'action':'ADJUST','adjustment':r['delta']} for r in rows]}}},
                timeout=(5,20),allow_redirects=False)
            # Even a batch with userErrors may have a partial outcome. Never replay it blindly.
            if response.status_code!=200:
                raise OrderchampError('MUTATION_OUTCOME_UNKNOWN')
            body=response.json()
            data=body.get('data',{}).get('inventoryLevelBulkAdjust') if isinstance(body,dict) else None
            if not isinstance(data,dict) or body.get('errors') or data.get('userErrors'):
                raise OrderchampError('MUTATION_OUTCOME_UNKNOWN')
            levels=data.get('inventoryLevels')
            if not isinstance(levels,list) or len(levels)!=len(rows):
                raise OrderchampError('MUTATION_OUTCOME_UNKNOWN')
            if any(not isinstance(r,dict) or type(r.get('quantity')) is not int or type(r.get('availableQuantity')) is not int for r in levels):
                raise OrderchampError('MUTATION_OUTCOME_UNKNOWN')
            if {r.get('id') for r in levels}!={r['level_id'] for r in rows} or data.get('clientMutationId')!=mutation_id:
                raise OrderchampError('MUTATION_OUTCOME_UNKNOWN')
            return levels
        except (requests.RequestException,ValueError,TypeError,KeyError):
            raise OrderchampError('MUTATION_OUTCOME_UNKNOWN') from None
        finally:
            if response is not None: response.close()

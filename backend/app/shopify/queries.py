"""GraphQL Admin API documents used by Trekiva.

All Shopify GraphQL lives here and in `app.shopify.fulfillment`, so an API-version change touches
only these files. Validate against the pinned SHOPIFY_API_VERSION when bumping it.
"""

ORDER_FOR_LOGISTICS = """
query OrderForLogistics($id: ID!) {
  order(id: $id) {
    id
    name
    createdAt
    cancelledAt
    closed
    tags
    email
    phone
    displayFinancialStatus
    displayFulfillmentStatus
    paymentGatewayNames
    currencyCode
    totalPriceSet { shopMoney { amount currencyCode } }
    totalOutstandingSet { shopMoney { amount currencyCode } }
    customer { displayName }
    shippingAddress {
      name company address1 address2 city province provinceCode zip country countryCodeV2 phone
    }
    risk {
      recommendation
      assessments { riskLevel }
    }
    fulfillmentOrders(first: 50, includeClosed: true) {
      nodes {
        id
        status
        requestStatus
        fulfillAt
        deliveryMethod { methodType }
        assignedLocation { name location { id } }
        fulfillmentHolds { id reason reasonNotes displayReason }
        lineItems(first: 250) {
          nodes {
            id
            sku
            productTitle
            variantTitle
            remainingQuantity
            totalQuantity
            weight { unit value }
            lineItem { id }
          }
        }
      }
    }
  }
}
"""

FULFILLMENT_ORDER_PARENT = """
query FulfillmentOrderParent($id: ID!) {
  fulfillmentOrder(id: $id) {
    id
    order { id }
  }
}
"""

FULFILLMENT_ORDER_FULFILLMENTS = """
query FulfillmentOrderFulfillments($id: ID!) {
  fulfillmentOrder(id: $id) {
    id
    status
    fulfillments(first: 20) {
      nodes { id status trackingInfo { company number url } }
    }
  }
}
"""

# Omitting fulfillmentOrderLineItems fulfills every remaining line item of the fulfillment order.
FULFILLMENT_CREATE = """
mutation TrekivaFulfillmentCreate($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id status trackingInfo { company number url } }
    userErrors { field message }
  }
}
"""

FULFILLMENT_EVENT_CREATE = """
mutation TrekivaFulfillmentEventCreate($fulfillmentEvent: FulfillmentEventInput!) {
  fulfillmentEventCreate(fulfillmentEvent: $fulfillmentEvent) {
    fulfillmentEvent { id status }
    userErrors { field message }
  }
}
"""

FULFILLMENT_CANCEL = """
mutation TrekivaFulfillmentCancel($id: ID!) {
  fulfillmentCancel(id: $id) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""

TAGS_ADD = """
mutation TrekivaTagsAdd($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) {
    node { id }
    userErrors { field message }
  }
}
"""

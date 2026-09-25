import plaid
from plaid.api import plaid_api
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from app.config import settings

_ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


def _client() -> plaid_api.PlaidApi:
    host = _ENV_MAP.get(settings.plaid_env.lower(), plaid.Environment.Sandbox)
    configuration = plaid.Configuration(
        host=host,
        api_key={
            "clientId": settings.plaid_client_id,
            "secret": settings.plaid_secret,
        },
    )
    api_client = plaid.ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)


client = _client()


def create_link_token(user_id: str) -> str:
    # Only "transactions" is required to even show an institution in Link — other
    # configured products (e.g. "liabilities") are requested opportunistically via
    # required_if_supported_products, so institutions that don't support them
    # (Webull, Bilt, etc.) aren't excluded from Link entirely.
    products = settings.plaid_products_list
    required = [Products("transactions")] if "transactions" in products else []
    opportunistic = [Products(p) for p in products if p != "transactions"]

    request = LinkTokenCreateRequest(
        products=required,
        required_if_supported_products=opportunistic,
        client_name="Personal Finance Tracker",
        country_codes=[CountryCode(c) for c in settings.plaid_country_codes_list],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id=user_id),
    )
    response = client.link_token_create(request)
    return response.link_token


def exchange_public_token(public_token: str) -> tuple[str, str]:
    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    return response.access_token, response.item_id


def get_institution_name(access_token: str) -> str:
    item_response = client.item_get(ItemGetRequest(access_token=access_token))
    institution_id = item_response.item.institution_id
    if not institution_id:
        return ""
    inst_response = client.institutions_get_by_id(
        InstitutionsGetByIdRequest(
            institution_id=institution_id,
            country_codes=[CountryCode(c) for c in settings.plaid_country_codes_list],
        )
    )
    return inst_response.institution.name


def get_accounts(access_token: str) -> list:
    response = client.accounts_get(AccountsGetRequest(access_token=access_token))
    return response.accounts


def remove_item(access_token: str) -> None:
    client.item_remove(ItemRemoveRequest(access_token=access_token))


def get_liabilities(access_token: str) -> list:
    response = client.liabilities_get(LiabilitiesGetRequest(access_token=access_token))
    return response.liabilities.credit or []


def get_holdings(access_token: str) -> tuple[list, list]:
    response = client.investments_holdings_get(InvestmentsHoldingsGetRequest(access_token=access_token))
    return response.securities or [], response.holdings or []


def sync_transactions(access_token: str, cursor: str | None) -> dict:
    added, modified, removed = [], [], []
    has_more = True
    while has_more:
        request = TransactionsSyncRequest(access_token=access_token)
        if cursor:
            request.cursor = cursor
        response = client.transactions_sync(request)
        added.extend(response.added)
        modified.extend(response.modified)
        removed.extend(response.removed)
        has_more = response.has_more
        cursor = response.next_cursor
    return {"added": added, "modified": modified, "removed": removed, "cursor": cursor}

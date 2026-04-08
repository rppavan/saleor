import pytest


@pytest.mark.parametrize(
    ("_case", "is_valid", "query"),
    [
        (
            "Mutation count lower than the limit should be allowed",
            True,
            """
            mutation {
                tokenCreate(email: "x", password: "x") { __typename }
            }
            """,
        ),
        (
            "Too many mutations -> should reject",
            False,
            """
            mutation {
                tokenCreate(email: "x", password: "x") { __typename }
                tokenVerify(token: "x") { __typename }
                tokenRefresh(refreshToken: "x") { __typename }
            }
            """,
        ),
        (
            "Mutations should be counted even when using aliases",
            False,
            """
            mutation {
                tokenCreate(email: "x", password: "x") { __typename }
                alias: tokenCreate(email: "x", password: "x") { __typename }
                alias2: tokenCreate(email: "x", password: "x") { __typename }
            }
            """,
        ),
        (
            "Should count even if using multiple operations",
            False,
            """
            mutation {
                tokenCreate(email: "x", password: "x") { __typename }
            }
            mutation {
                tokenCreate(email: "x", password: "x") { __typename }
            }
            mutation {
                tokenCreate(email: "x", password: "x") { __typename }
            }
            """,
        ),
    ],
)
def test_limits_number_of_aliases(
    api_client, settings, _case: str, is_valid: bool, query: str
):
    # Prevent alias limiter from triggering as we want to test alias validation
    settings.GRAPHQL_ALIAS_COUNT_LIMIT = 10
    settings.GRAPHQL_MUTATION_COUNT_LIMIT = 2

    # When sending a batch with only 1 query, it should allow it
    resp = api_client.post(data={"query": query})
    resp_data = resp.json()
    resp_data.pop("extensions")

    if is_valid:
        assert "errors" not in resp_data
        assert resp.status_code == 200
    else:
        assert resp_data == {
            "errors": [
                {
                    "extensions": {"exception": {"code": "GraphQLError"}},
                    "message": "Number of mutations exceed the limit of 2",
                }
            ]
        }
        assert resp.status_code == 400

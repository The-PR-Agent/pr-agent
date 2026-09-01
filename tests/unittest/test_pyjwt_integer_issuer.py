import jwt


class TestPyJWTIntegerIssuer:
    def test_encode_accepts_integer_issuer(self):
        """GitHub App JWTs require an integer 'iss' claim; PyJWT >=2.11 rejects it.

        Guards the PyJWT pin (#2955, previously #2210): if a dependency bump
        installs a PyJWT that raises "Issuer (iss) must be a string", GitHub
        App authentication breaks.
        """
        token = jwt.encode({"iss": 123456, "exp": 9999999999}, "secret", algorithm="HS256")
        assert jwt.decode(token, "secret", algorithms=["HS256"])["iss"] == 123456

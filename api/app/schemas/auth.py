import uuid

from pydantic import BaseModel, EmailStr, Field


class RegisterIn(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    # uuid.UUID, not str — Pydantic v2 does not coerce UUID objects to str.
    # The JSON encoder serializes UUID -> string on the wire automatically.
    id: uuid.UUID
    email: str
    full_name: str
    role: str

    model_config = {"from_attributes": True}


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds

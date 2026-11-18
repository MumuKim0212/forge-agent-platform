from pydantic import BaseModel, ConfigDict


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    stack: str
    dockerfile_ref: str
    deploy_config_ref: str
    policy_profile: str
    version: str
    enabled: bool

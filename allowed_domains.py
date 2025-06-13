import os
from typing import List, Optional
from pydantic import BaseModel, HttpUrl, field_validator
import re

class DomainConfig(BaseModel):
    """Configuration for allowed domains"""
    allowed_domains: List[str] = [
        "localhost",
        "127.0.0.1",
        "template.online",
        "www.template.online"
    ]
    
    # Domains that are allowed for video URLs
    allowed_video_domains: List[str] = [
        "youtube.com",
        "youtu.be",
        "www.youtube.com"
    ]
    
    @field_validator('allowed_domains')
    def validate_domains(cls, v):
        """Validate domain format"""
        for domain in v:
            if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9-]{1,61}[a-zA-Z0-9]\.[a-zA-Z]{2,}$', domain) and domain not in ['localhost', '127.0.0.1']:
                raise ValueError(f"Invalid domain format: {domain}")
        return v

def get_allowed_origins() -> List[str]:
    """Get list of allowed origins for CORS"""
    config = DomainConfig()
    return [f"http://{host}" for host in config.allowed_domains] + [f"https://{host}" for host in config.allowed_domains]

def is_allowed_domain(domain: str) -> bool:
    """Check if a domain is allowed for API access"""
    config = DomainConfig()
    return domain in config.allowed_domains

def is_allowed_video_domain(domain: str) -> bool:
    """Check if a domain is allowed for video URLs"""
    config = DomainConfig()
    return domain in config.allowed_video_domains

def validate_url(url: str) -> bool:
    """Validate if URL is from allowed domain"""
    try:
        parsed_url = HttpUrl(url)
        domain = parsed_url.host
        
        # Check if it's a YouTube URL
        if any(yt_domain in domain for yt_domain in ['youtube.com', 'youtu.be']):
            return True
            
        # For other URLs, check against allowed domains
        return is_allowed_domain(domain)
    except Exception:
        return False

# Load domains from environment variable if available
if os.getenv("ALLOWED_DOMAINS"):
    try:
        domains = os.getenv("ALLOWED_DOMAINS").split(",")
        DomainConfig(allowed_domains=domains)
    except Exception as e:
        print(f"Error loading domains from environment: {e}") 
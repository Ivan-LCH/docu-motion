"""
TTS Engine - Remote API Client
Calls the remote Qwen3-TTS server instead of loading model locally.
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)


class TTSEngine:
    """Remote TTS API Client for Voice Cloning"""
    
    def __init__(self, server_url=None, voice_name=None):
        """
        Initialize TTS Engine with remote server configuration.
        
        Args:
            server_url: TTS server URL (default: from env TTS_SERVER_URL)
            voice_name: Registered voice name (default: from env TTS_VOICE_NAME)
        """
        self.server_url = server_url or os.getenv("TTS_SERVER_URL", "http://localhost:8002")
        self.voice_name = voice_name or os.getenv("TTS_VOICE_NAME", "myvoice")
        self.timeout = 300  # 5 minutes timeout for TTS generation
        
        logger.info(f"TTS Engine initialized: server={self.server_url}, voice={self.voice_name}")
    
    def health_check(self):
        """Check if the remote server is available."""
        try:
            response = requests.get(f"{self.server_url}/health", timeout=5)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False
    
    def generate(self, text, output_file, ref_audio_path=None, ref_text=None, language="Auto"):
        """
        Generate speech from text using remote TTS API.
        Includes retry logic for temporary connection failures.
        
        Args:
            text: Text to synthesize
            output_file: Path to save the output audio
            ref_audio_path: (Ignored - uses server-side registered voice)
            ref_text: (Ignored - uses server-side registered voice)
            language: Language setting (passed to server)
        
        Returns:
            bool: True if successful, False otherwise
        """
        import time
        
        max_retries = 3
        retry_delay = 2  # seconds
        
        # Prepare request data
        data = {
            "text": text,
            "voice_name": self.voice_name,
            "language": language
        }
        
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"Generating TTS via remote API (attempt {attempt}/{max_retries}): {text[:50]}...")
                
                # Make API request
                response = requests.post(
                    f"{self.server_url}/generate",
                    data=data,
                    timeout=self.timeout
                )
                
                if response.status_code != 200:
                    logger.error(f"TTS API error: {response.status_code} - {response.text}")
                    return False
                
                # Save audio file
                with open(output_file, "wb") as f:
                    f.write(response.content)
                
                logger.info(f"Saved audio to {output_file}")
                return True
                
            except requests.exceptions.Timeout:
                logger.error(f"TTS request timed out after {self.timeout}s")
                return False  # Don't retry on timeout (already waited long)
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"Connection failed (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    logger.info(f"Retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"Failed to connect to TTS server after {max_retries} attempts: {self.server_url}")
                    return False
            except Exception as e:
                logger.error(f"TTS Generation failed: {e}")
                return False
        
        return False

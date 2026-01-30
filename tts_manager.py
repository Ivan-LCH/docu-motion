import os
import gc
import torch
import logging
import soundfile as sf
import warnings
import subprocess
import shutil

# Suppress warnings
warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)

class TTSEngine:
    """Qwen3-TTS Engine for Voice Cloning"""
    
    def __init__(self, model_id="Qwen/Qwen3-TTS-12Hz-0.6B-Base", device="cpu"):
        self.model_id = model_id
        self.device = device
        self.model = None
        self._load_model()

    def _load_model(self):
        """Load Qwen3-TTS model using from_pretrained()"""
        try:
            logger.info(f"Loading Qwen3-TTS model: {self.model_id} on {self.device}")
            from qwen_tts import Qwen3TTSModel
            
            self.model = Qwen3TTSModel.from_pretrained(
                self.model_id,
                device_map=self.device,
                dtype=torch.float32,  # CPU 환경에서는 float32 권장
            )
            logger.info("Model loaded successfully.")
        except ImportError:
            logger.error("Failed to import qwen_tts. Please ensure 'pip install -U qwen-tts' was run.")
            raise
        except Exception as e:
            logger.error(f"Failed to load model: {e}")
            raise

    def _convert_audio_to_wav(self, input_path):
        """Convert any audio to 16kHz/24kHz mono wav using ffmpeg"""
        try:
            # Create temp output path
            base, _ = os.path.splitext(input_path)
            output_path = f"{base}_temp_converted.wav"
            
            # Remove existing temp file if any
            if os.path.exists(output_path):
                os.remove(output_path)
                
            logger.info(f"Converting audio to WAV: {input_path} -> {output_path}")
            
            # Run ffmpeg command
            # -i input -ac 1 (mono) -ar 16000 (sample rate) -y (overwrite)
            cmd = [
                "ffmpeg", 
                "-i", input_path, 
                "-ac", "1", 
                "-ar", "24000", # Qwen typically likes 16k or 24k
                "-y", 
                output_path
            ]
            
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return output_path
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg conversion failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Audio conversion error: {e}")
            return None

    def generate(self, text, output_file, ref_audio_path=None, ref_text=None, language="Auto"):
        """
        Generate speech from text with optional voice cloning.
        Auto-converts .m4a references to .wav.
        """
        if not self.model:
            logger.error("Model is not loaded.")
            return False
            
        temp_wav_path = None

        try:
            logger.info(f"Generating TTS for text: {text[:50]}...")
            
            # Debug path
            abs_ref_path = os.path.abspath(ref_audio_path) if ref_audio_path else "None"
            logger.info(f"Checking Reference Audio Path: {ref_audio_path} (Absolute: {abs_ref_path}, Exists: {os.path.exists(ref_audio_path) if ref_audio_path else False})")
            
            if ref_audio_path and os.path.exists(ref_audio_path):
                # Check for conversion need
                file_ext = os.path.splitext(ref_audio_path)[1].lower()
                final_ref_path = ref_audio_path
                
                if file_ext == ".m4a":
                    temp_wav_path = self._convert_audio_to_wav(ref_audio_path)
                    if temp_wav_path:
                        final_ref_path = temp_wav_path
                    else:
                        logger.warning("Failed to convert m4a. attempting to use original file.")

                # Voice Cloning Mode
                logger.info(f"Using Voice Clone with reference audio: {final_ref_path}")
                wavs, sr = self.model.generate_voice_clone(
                    text=text,
                    language=language,
                    ref_audio=final_ref_path,
                    ref_text=ref_text,
                )

            else:
                # Default Mode (No Reference Audio)
                # Base 모델은 Voice Clone 전용이므로 ref_audio 없이는 제한적일 수 있음
                # 대안: 기본 합성 시도 또는 Edge-TTS로 폴백
                logger.warning("No reference audio found. Attempting default generation (may be limited for Base model).")
                try:
                    # Try generate_voice_clone with empty ref (if API supports)
                    wavs, sr = self.model.generate_voice_clone(
                        text=text,
                        language=language,
                        ref_audio=None,
                        ref_text=None,
                    )
                except Exception as e:
                    logger.error(f"Default generation failed: {e}")
                    return False
            
            # Save output (wavs is typically a list, take first element)
            audio_data = wavs[0] if isinstance(wavs, list) else wavs
            sf.write(output_file, audio_data, sr)
            logger.info(f"Saved audio to {output_file} (sample_rate={sr})")
            
            return True

        except Exception as e:
            logger.error(f"TTS Generation failed: {e}")
            return False
        finally:
            # Memory Management
            gc.collect()
            # Clean up temp file
            if temp_wav_path and os.path.exists(temp_wav_path):
                try:
                    os.remove(temp_wav_path)
                    logger.info(f"Removed temp converted file: {temp_wav_path}")
                except:
                    pass

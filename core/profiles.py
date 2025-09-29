"""Channel profile loader and models"""
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Literal, Any
from pathlib import Path
import yaml
import logging

logger = logging.getLogger(__name__)


class EngineConfig(BaseModel):
    """Engine configuration"""
    type: Literal["codex", "claude-code"]
    config_home: str
    timeout: int = 600


class VoiceConfig(BaseModel):
    """Voice configuration for channel"""
    enabled: bool = False
    voice_channel_ids: Optional[List[str]] = None
    stt_provider: str = "whisper"
    tts_provider: str = "kokoro"
    tts_fallback: Optional[str] = "piper"
    tts_voice: str = "af_sky"
    silence_threshold: float = 1.5
    stream_chunks: bool = True


class SessionConfig(BaseModel):
    """Session configuration"""
    persist: bool = True
    timeout: int = 3600
    transcript_store: bool = True


class MCPServerConfig(BaseModel):
    """MCP server configuration"""
    name: str
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class ChannelProfile(BaseModel):
    """Complete channel profile configuration"""
    channel: Dict[str, Any]
    engine: EngineConfig
    voice: Optional[VoiceConfig] = None
    session: SessionConfig
    mcp_servers: List[MCPServerConfig] = Field(default_factory=list)


class ProfileLoader:
    """Loads and manages channel profiles"""

    def __init__(self, profile_dir: Path):
        self.profile_dir = profile_dir
        self.profiles: Dict[str, ChannelProfile] = {}
        self.channel_map: Dict[str, str] = {}  # channel_id/name -> profile_key
        self.voice_channel_map: Dict[str, tuple[str, str]] = {}  # voice_channel_id -> (profile_key, text_channel_id)
        self._load_profiles()

    def _load_profiles(self):
        """Load all YAML profiles from directory"""
        if not self.profile_dir.exists():
            logger.warning(f"Profile directory not found: {self.profile_dir}")
            return

        for profile_file in self.profile_dir.glob("*.yaml"):
            try:
                with open(profile_file) as f:
                    data = yaml.safe_load(f)

                profile = ChannelProfile(**data)
                profile_key = profile_file.stem

                # Map channel IDs to this profile
                for channel_id in profile.channel.get("ids", []):
                    self.channel_map[str(channel_id)] = profile_key

                # Map channel names to this profile
                for channel_name in profile.channel.get("names", []):
                    self.channel_map[channel_name.lower()] = profile_key

                # Map voice channel IDs to this profile
                if profile.voice and profile.voice.enabled:
                    if profile.voice.voice_channel_ids:
                        text_channel_ids = profile.channel.get("ids", [])
                        if not text_channel_ids:
                            raise ValueError(
                                f"Profile {profile_key}: voice.enabled requires at least one channel.ids"
                            )

                        text_channel_id = str(text_channel_ids[0])

                        for voice_channel_id in profile.voice.voice_channel_ids:
                            voice_channel_id = str(voice_channel_id)

                            if voice_channel_id in self.voice_channel_map:
                                existing_profile, _ = self.voice_channel_map[voice_channel_id]
                                raise ValueError(
                                    f"Voice channel ID collision: {voice_channel_id} "
                                    f"claimed by both {existing_profile} and {profile_key}"
                                )

                            self.voice_channel_map[voice_channel_id] = (profile_key, text_channel_id)

                        logger.info(f"Mapped {len(profile.voice.voice_channel_ids)} voice channel(s) to {profile_key}")

                self.profiles[profile_key] = profile
                id_count = len(profile.channel.get("ids", []))
                name_count = len(profile.channel.get("names", []))
                logger.info(f"Loaded profile: {profile_key} ({id_count} IDs, {name_count} names)")

            except Exception as e:
                logger.error(f"Error loading profile {profile_file}: {e}", exc_info=True)

    def get_profile(self, channel_identifier: str) -> Optional[ChannelProfile]:
        """Get profile for channel ID or name"""
        # Try exact match first (for IDs)
        profile_key = self.channel_map.get(channel_identifier)

        # Try lowercase match (for names)
        if not profile_key:
            profile_key = self.channel_map.get(channel_identifier.lower())

        if not profile_key:
            logger.warning(f"No profile found for channel: {channel_identifier}")
            return None

        return self.profiles.get(profile_key)

    def get_profile_for_voice_channel(self, voice_channel_id: str) -> Optional[tuple[str, ChannelProfile]]:
        """Get profile and text channel ID for voice channel ID

        Returns:
            Tuple of (text_channel_id, profile) if found, None otherwise
        """
        voice_channel_id = str(voice_channel_id)
        mapping = self.voice_channel_map.get(voice_channel_id)

        if not mapping:
            return None

        profile_key, text_channel_id = mapping
        profile = self.profiles.get(profile_key)

        if not profile:
            logger.error(f"Profile key {profile_key} not found for voice channel {voice_channel_id}")
            return None

        return (text_channel_id, profile)

    def reload(self):
        """Reload all profiles"""
        logger.info("Reloading channel profiles")
        self.profiles.clear()
        self.channel_map.clear()
        self.voice_channel_map.clear()
        self._load_profiles()

    def list_channels(self) -> List[str]:
        """List all configured channel names"""
        return list(self.channel_map.keys())
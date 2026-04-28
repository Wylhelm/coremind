"""Plugin host — gRPC server implementing the CoreMindHost service.

The plugin host is the daemon-side half of the plugin protocol. It accepts
connections from plugin processes, validates their manifests, maintains
lifecycle state, and routes events onto the internal EventBus.
"""

use anyhow::Result;
use crate::config::LoggingConfig;
use tracing_subscriber::{fmt, layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};

/// Initialize the tracing/logging subsystem
pub fn init_logging(config: &LoggingConfig) -> Result<()> {
    // Create log directory if it doesn't exist
    if let Some(parent) = std::path::Path::new(&config.file_path).parent() {
        std::fs::create_dir_all(parent)?;
    }

    let env_filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new(&config.level));

    if config.json_format {
        // Structured JSON logging for production
        tracing_subscriber::registry()
            .with(env_filter)
            .with(
                fmt::layer()
                    .json()
                    .with_target(true)
                    .with_thread_ids(true)
                    .with_current_span(true),
            )
            .init();
    } else {
        // Human-readable for development
        tracing_subscriber::registry()
            .with(env_filter)
            .with(
                fmt::layer()
                    .with_target(true)
                    .with_thread_ids(false)
                    .compact(),
            )
            .init();
    }

    tracing::info!(
        "Logging initialized at level '{}' (json={})",
        config.level,
        config.json_format
    );

    Ok(())
}

/// Log a structured trade event for audit trail
#[macro_export]
macro_rules! audit_log {
    ($event:expr, $($fields:tt)*) => {
        tracing::info!(
            target: "audit",
            event = $event,
            $($fields)*
        );
    };
}

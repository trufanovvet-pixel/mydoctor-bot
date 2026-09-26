import enhanced_app2
import clinical_patch
import history_patch
import media_patch
import notification_patch
import onboarding_patch
import web_search_patch
import clinic_ranking_patch
import activity_patch
import long_message_patch
import user_guide_patch
import pet_records_patch
import prevention_patch
import records_hub_patch
import terminology_patch
import voice_transcription_patch
import prevention_smart_patch
import prevention_time_patch
import voice_router_patch
import media_direct_client_patch
import medical_memory_patch
import feedback_patch
import web_login_patch


_original_install = enhanced_app2.enhanced.install_features
_original_clean_menu = enhanced_app2.install_clean_menu


def _install_with_patches(bot):
    _original_install(bot)
    notification_patch.install(bot)
    media_patch.install(bot)
    history_patch.install(bot)
    clinical_patch.install(bot)
    voice_transcription_patch.install(bot)


def _install_clean_menu_with_onboarding(bot):
    _original_clean_menu(bot)
    long_message_patch.install()
    onboarding_patch.install(bot)
    user_guide_patch.install(bot)
    pet_records_patch.install(bot)
    prevention_patch.install(bot)
    records_hub_patch.install(bot)
    terminology_patch.install(bot)
    prevention_smart_patch.install(bot)
    prevention_time_patch.install()
    clinic_ranking_patch.install()
    web_search_patch.install(bot)
    media_direct_client_patch.install(bot)
    activity_patch.install(bot)
    voice_router_patch.install(bot)
    medical_memory_patch.install(bot)
    feedback_patch.install(bot)
    web_login_patch.install(bot)
    import operations_patch
    operations_patch.install(bot)
    import telegram_billing
    telegram_billing.install(bot)
    import telegram_analytics
    telegram_analytics.install(bot)


enhanced_app2.enhanced.install_features = _install_with_patches
enhanced_app2.install_clean_menu = _install_clean_menu_with_onboarding


if __name__ == "__main__":
    enhanced_app2.main()

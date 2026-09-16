import enhanced_app2
import clinical_patch
import history_patch
import media_patch
import notification_patch
import onboarding_patch
import web_search_patch
import activity_patch


_original_install = enhanced_app2.enhanced.install_features
_original_clean_menu = enhanced_app2.install_clean_menu


def _install_with_patches(bot):
    _original_install(bot)
    notification_patch.install(bot)
    media_patch.install(bot)
    history_patch.install(bot)
    clinical_patch.install(bot)


def _install_clean_menu_with_onboarding(bot):
    _original_clean_menu(bot)
    onboarding_patch.install(bot)
    web_search_patch.install(bot)
    activity_patch.install(bot)


enhanced_app2.enhanced.install_features = _install_with_patches
enhanced_app2.install_clean_menu = _install_clean_menu_with_onboarding


if __name__ == "__main__":
    enhanced_app2.main()

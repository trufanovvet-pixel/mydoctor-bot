import enhanced_app2
import media_patch
import notification_patch


_original_install = enhanced_app2.enhanced.install_features


def _install_with_patches(bot):
    _original_install(bot)
    notification_patch.install(bot)
    media_patch.install(bot)


enhanced_app2.enhanced.install_features = _install_with_patches


if __name__ == "__main__":
    enhanced_app2.main()

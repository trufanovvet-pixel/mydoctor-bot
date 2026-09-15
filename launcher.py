import enhanced_app2
import notification_patch


_original_install = enhanced_app2.enhanced.install_features


def _install_with_notification_patch(bot):
    _original_install(bot)
    notification_patch.install(bot)


enhanced_app2.enhanced.install_features = _install_with_notification_patch


if __name__ == "__main__":
    enhanced_app2.main()

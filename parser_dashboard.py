        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          ZK_PASSWORD: ${{ secrets.ZK_PASSWORD }}
          YANDEX_TOKEN: ${{ secrets.YANDEX_TOKEN }}      # ⬅️ добавить
          ZK_LOGIN: 'ka'
          GITHUB_USERNAME: 'SpritePetrovich'
          GITHUB_REPO: 'monitoring-dashboard'
          GITHUB_BRANCH: 'main'
          RECENTLY_SINCE: '11.2026'
          YANDEX_REMOTE_FOLDER: 'disk:/__smart_place__/maps/2026'
          STALE_THRESHOLD_DAYS: '7'

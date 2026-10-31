# 正規 Reader の復旧と Awai 分散配信の実機確認

2026-11-01 JST。追跡: [JUN-228](https://linear.app/kotobalabs/issue/JUN-228)、
[JUN-23](https://linear.app/kotobalabs/issue/JUN-23)、
[AWAI-102](https://linear.app/kotobalabs/issue/AWAI-102)、
[AWAI-127](https://linear.app/kotobalabs/issue/AWAI-127)。

## 結論と実測

CID を共通の識別子にして HTTPS Reader と libp2p 配信を併用できる。
ただし現在の実機では正規 DNS/TLS、複数 provider の配信、障害時継続は未成立。
下表は在庫数や設計上の可能性ではなく、今回の取得結果である。
再確認で失敗した結果も残し、最初の成功を現在の健全性とみなさない。

| 経路 | 結果 | 証明できないもの |
| --- | --- | --- |
| Xavier Reader、SSH/Tailscale 転送、最初の3 CID | 各262,158 bytes、全3件 CID/SHA-256一致。6.680 / 9.227 / 12.063秒 | 全在庫の保持、独立WAN、稼働効率の改善 |
| Jacob Reader、同じ3 CID | 12秒の期限で全件 timeout。長い期限の再確認では約15秒で503 | 配信可能な第二 replica |
| Jacob native Kubo、最初の CID を offline 読取 | 262,158 bytes、CID一致、0.100秒。ただし direct/all の pin確認は「not pinned」 | 永続保持、HTTPサービス品質 |
| Xavier の Reader をホスト内で再確認 | 200、262,158 bytes、0.172秒（この curl はhash未照合） | クライアントまでの取得成功 |
| 新しい期限付き測定ツール、両Reader | Xavier は200の後に18秒でbody取得timeout、Jacob503。Xavier単独再確認も8秒timeout | 継続的なHTTP配信、改善率 |
| public IPv4 native libp2p | Peer接続が一度成功したが、3件のBitswap取得は各18秒で全失敗。その後のTCP/QUIC/stream再確認も失敗 | DNS/Tailscale非依存の配信成功 |

public IP への試行元は同じ LAN の Mac。独立WANの疎通試験ではない。
完全な観測記録は [evidence/jun-228-20261101.json](evidence/jun-228-20261101.json)。
最初のXavier成功合計は786,474 bytes。件数が少なく、再確認失敗もあるため
スループット向上・稼働率向上・SLO達成を主張しない。改善率は **未測定**。

## 正規入口が止まる理由

1. authoritative Cloudflare zone は active だが、`ipfs.yataverse.com` 自体の
   A/AAAA/CNAMEがない。`*.ipfs.yataverse.com` の proxied `AAAA 100::` は
   残っているが、IPFS用 Worker route/custom domain はない。
2. 既存の暫定SSLIP名と443 renewal scriptは `220.146.170.114` に固定。
   ルーターの現在値は `220.146.177.126`。
3. `/usr/local/sbin/kotoba-xavier-https-443-map` はWAN変更時に意図どおり拒否する。
   timerは稼働しているがserviceは失敗。80/8443側の更新scriptも同じ旧IPを要求する。
4. UPnPの個別照会で80・443・8443は714（転送なし）。ホスト内の既存TLS/Readerは応答する。
   DNSだけ変更しても正規HTTPSは直らない。
5. canonical証明書は未取得。準備のcertbot呼出しはSSH banner timeoutで失敗し、
   DNS challenge、証明書配備、公開vhost切替は実施していない。

## この変更で用意したもの

- `deploy/renew_public_https.sh`: 標準はdry-run。レビュー済みWANを必須にし、
  XavierのLANアドレス、既存nginx、既存ReaderのTLSと在庫identity、443所有者を確認。
  明示apply時だけ同じXavierの443 leaseを更新し、所有者を再照会する。
  他の所有者の転送削除、80/8443変更、自動的な新WAN受入れはしない。
- `deploy/jun228-https-map.conf.candidate`: 既存443 timer向けのservice drop-in案。
  未配備。再びWANが変われば停止し、次のレビューを要求する。
- `deploy/ipfs-yataverse-canonical.conf.candidate`: protocol service専用の
  `ipfs.yataverse.com` vhost案。GET/OPTIONSと限定したReader経路のみ。
  アプリ実行や任意のupstream fetch、失敗時の別gateway転送を加えない。
- `deploy/ipfs-yataverse-dns.json.candidate`: DNS-only A、TTL300秒の案。
  Cloudflareをbytes配信経路に置かない。値は観測時点の候補であり再確認必須。
- `deploy/probe_reader.py`: 明示した最大4 providerを一件ずつ測定するPOSIX運用CLI。
  最大8MiB、最大60秒の実時間期限、CID/サイズ照合、redirect拒否、HTTPは数値loopbackのみ。
  proxy環境変数による迂回をしない。アプリruntimeやnative gateではない。

実機dry-runは成功し、ルーターへの書込みなし。
candidate Nginx構文は既存SSLIP証明書のパスに一時置換した独立設定で成功。
これはcanonical証明書のSAN/TLS検証ではない。
4つの運用テストでCID不一致、過大body、redirect拒否、期限、WAN drift、
他の443所有者、曖昧な所有者、失敗したlisting、apply後の所有者不一致を検証した。

```sh
cd deploy
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_jun228_operations
sh -n renew_public_https.sh
```

## 本番切替の具体案と中止条件

共有AGENTS.mdのproduction route cutover承認を別途得た後に、次を順に行う。
データの互換性は現行 `/ipfs/{cid}` のraw block契約を維持し、CID照合で判定する。

1. WANと443所有者を再読取。candidate scriptを再dry-run。
   観測値が変わっていればこのIPを公開せず中止し、差分を再レビューする。
2. レビュー済みscript/drop-inで既存Xavierの443入口だけ復旧。
   既存timerの間隔と共有SNIを維持し、別hostへの移動や80/8443奪取はしない。
3. 既存ACME accountでcanonical SANの証明書を取得し、digest/期限/SANを確認。
   DNS-01ならtask所有のTXTだけを使用・後片付けする。challengeや秘密鍵をcommitしない。
   自動renewal経路が未成立なら、その残課題と期限監視も記録する。
4. 証明書を使ってcandidateを `nginx -t`、canonical SNIをローカルで照合してからreload。
   `curl --resolve` でcanonical URLと3 CIDを検証し、独立WANからも照合する。
   複数providerが未成立でも「単一Readerの復旧」として正確に扱う。
5. 取得成功後だけDNS-only Aを公開。authoritativeと外部resolverからDNS、通常HTTPS、
   CIDを再照合。既存wildcardは別のsubdomain契約なので、この作業で一括変更しない。

中止/rollback: 原因を記録し、taskが追加したDNS record ID、canonical vhostと
drop-inだけを復元する。共有443のleaseや無関係なSNI/serviceを無条件削除しない。
DNS cacheはTTLを待って再確認。apply失敗でも途中でleaseが作成された可能性があるので、
scriptの終了コードだけで「変更なし」とせず必ず所有者を読み戻す。

## Awai上で分散配信を成立させる順序

Awaiはproviderの探索・権限・余力・割当、YataverseはCID bytes、Kuraは保持と修復を
担う設計にする。IPFSのCID、routing、Bitswap/libp2pは利用できる実装部品だが、
planner/gossipのpure rulesが存在するだけでは実transport稼働を証明しない。
Xavierには `/x/yataverse/lake-reader/1` と `/x/yataverse/graph-head/1` のmountが
実在した。一方、JacobのKubo AddrFiltersはTailscaleの100.64.0.0/10を拒否する。
filter全解除は行わず、明示peer認証と限定route、または検証済みpublic/relay経路を選ぶ。

1. 同じ小さな3 CIDを独立した2ホストでsize/hash照合し、各ホストの保持receiptとpinを
   再確認。キャッシュにあるだけのbytesは保持容量・報酬・replica数に数えない。
2. providerは署名済みidentity、CID/サイズ、保持期限、policy、取得期限、失効を宣言。
   署名・policy・receiptが未接続のものはAWAI-102/JUN-23の資格未達とする。
3. CPU/RAM/storage/networkの実測と予約を取得し、foreground優先・予備容量・同時実行上限で
   admissionする。今回の読取は逐次一件・小さいpayloadに限定したが、hard reserveではない。
   GPU負荷、電力、実アプリ遅延も未測定。
4. CID検証成功bytes/秒、p95、成功率、CPU秒/GiB、network bytes、複製費用を収集。
   コピーを増やしたbytesではなく、正しく完了した有用ジョブで効率を計る。
5. 一方のproviderをクライアント側の選択候補から外して、残るproviderだけで同一CIDを
   fresh取得。既存ホストやサービスを止める必要はない。
   実際の電源/回線障害試験と独立故障domainの証明は別に行う。
6. Murakumoの固定model実ジョブとYataverse読取を、単一node/配分後で同じ入力・余力制約・
   負荷条件で比較。latency/完了数/foreground SLO/電力の比較が揃うまで改善率は未確定。

ブラウザーに複数providerを組み込む場合はcanonical client-runtime brokerの宣言と
操作ごとの新しい同意を使う。別endpointへの暗黙fallbackや独自consent実装は加えない。
botの運用CLIの許可はブラウザーの同意の代わりにならない。

IPFSの[構成要素](https://docs.ipfs.tech/concepts/how-ipfs-works/)と
[Trustless Gateway仕様](https://specs.ipfs.tech/http-gateways/trustless-gateway/)を参照。
現行Readerはqueryを拒否する独自raw契約で、仕様上の全機能の適合を主張しない。

## コードと既存説明の不一致

READMEのService evidence build節はPRでJVM-free buildを行うと説明するが、
`scripts/build-service-evidence.mjs:40–57` はNBB `cli.js` を検証・起動する経路。
共有の明示指示に従い、そのworkflow定義を削除し、新しいAGENTS.mdに失った検査を記録。
native代替gateは未確立。README本文はowner判断のため変更していない。

`deploy/p2p_mesh.cljk:18–28` とservice unitのcontrollerはNode CLIであり、
実際のReaderもPythonが稼働していた。`.cljk`への移植やKubo native binaryの存在を
Awai全体のAmu native資格と混同しない。稼働中の既存サービスは保全した。

DNS/router/本番service/保持状態の変更はこの調査では **なし**。
task所有probeとforwardだけを終了し、共有WIPと他agentのworktreeを残す。

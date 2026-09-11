# 3D NAND 栅极后置流程（slab 内核示例）

`flow.json` 是一条完整的替代栅（gate-last）3D NAND 工艺流程，39 步，slab 内核可以直接跑。
结构按真实流程排列，尺寸比量产器件粗几倍，一台笔记本几分钟能算完：

| 结构 | 取值 |
| --- | --- |
| ON 对数 | 6 对，SiO₂ 25 nm / SiN 30 nm |
| 沟道孔 | 直径 110 nm，6 个，两列 |
| 缝隙（slit） | 120 nm 宽，两条，块夹在中间 |
| 阶梯 | 3 级，每级去掉一对 ON |
| 存储膜 | 阻挡氧化 6 nm / 俘获氮化 6 nm / 隧穿氧化 5 nm / 多晶硅沟道 8 nm |
| 字线 | Al₂O₃ 3 nm + TiN 3 nm + W |
| 采样 | z 步长 4 nm，XY 弦高 4 nm |

## 怎么跑

```bash
process-studio --root nand flow apply examples/3d-nand/flow.json   # 建工作目录并写入流程
process-studio --root nand run                                     # 运行全部 39 步
process-studio --root nand view section --named 1 -o holes.png     # 穿过沟道孔与缝隙的截面
process-studio --root nand view section --named 3 -o stairs.png    # 穿过阶梯与字线接触的截面
process-studio --root nand view mesh -o nand.glb                   # 3D 表面
```

`nand` 目录用桌面版的 Open workspace 打开即可在界面里逐步查看。三条截面线已经存在工程里（Section 视图的 Line 下拉）。

## 流程

1. **衬底与源极板**：n⁺ 多晶硅源极板 100 nm，刻蚀停止氧化层 20 nm。
2. **ONON 叠层**：6 对平面沉积，盖层氧化物 50 nm。
3. **沟道孔**：一次垂直刻穿叠层，响应表只有 SiO₂ 和 SiN，源极板不在表里所以自动停在它上面。
4. **存储膜与沟道**：ALD 阻挡氧化物、俘获氮化物、隧穿氧化物，LPCVD 多晶硅沟道，氧化物填满孔心，CMP 到盖层。
   俘获氮化物用单独的材料名 `SiN-trap`，后面湿法去牺牲 SiN 时不会碰它。
5. **阶梯**：三张掩膜各刻一对 ON（第一张连盖层一起），氧化物填平，CMP。
6. **缝隙**：两条缝隙一次刻穿，停在源极板。
7. **替代栅**：各向同性刻蚀只对 SiN 生效，从缝隙侧壁向内推进 0.27 µm，氧化层是屏障；
   随后 Al₂O₃、TiN、W 保形沉积填满空腔，W 各向同性回刻 40 nm 把缝隙里的钨去掉并让字线彼此断开；
   缝隙内衬氧化物、填多晶硅作为源极线，CMP。
8. **字线接触**：一次刻蚀打到三级台阶上，W 不在响应表里所以各自停在字线上；W 填充，CMP。

## 和真实流程的差别

- 孔底冲开（bottom punch）没有做：slab 内核的垂直刻蚀会把掩膜开口内侧壁上的膜一起去掉，做不出只开孔底的效果，所以沟道没有和源极板接通。
- 阶梯只有三级、一张掩膜一级；真实流程用修剪光刻胶在一张掩膜里做十几级。
- 层数、孔径和膜厚按能在几分钟内算完的尺度取的，可以在 `make_flow.py` 顶部改常数重新生成 `flow.json`。
- 各向同性去 SiN 是整条流程里最慢的一步：它按最薄非目标层厚度的一半步进，采样越细步数越多。

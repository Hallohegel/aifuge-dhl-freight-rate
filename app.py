# 更新后的Hellmann运输费用计算系统

# 定义每个国家的费率规则字典
hellmann_country_rules = {
    'Germany': {
        'Maut': 18.2,  # 德国境内Maut费率
        'Staatliche Abgaben': 0,  # 德国境内政府附加费
        'Frachtvolumenverhältnis': 150,  # 体积系数
        'Zusatzfee': {  # 额外费用
            'Gefahrgut': 15,  # 危险品附加费用
            'B2C': 8.9,  # B2C附加费用
            'Avis': 12.5,  # 电话预约派送费用
            'Längenzuschlag': 30,  # 货物长度大于240cm的附加费用
        }
    },
    'Austria': {
        'Maut': 13.3,  # 奥地利Maut费率
        'Staatliche Abgaben': 6.6,  # 奥地利政府附加费
        'Frachtvolumenverhältnis': 200,  # 体积系数
    },
    'Belgium': {
        'Maut': 9.7,  # 比利时Maut费率
        'Staatliche Abgaben': 2.1,  # 比利时政府附加费
        'Frachtvolumenverhältnis': 200,  # 体积系数
    },
    'Bulgaria': {
        'Maut': 6.2,  # 保加利亚Maut费率
        'Staatliche Abgaben': 9.9,  # 保加利亚政府附加费
        'Frachtvolumenverhältnis': 200,  # 体积系数
    },
    'Croatia': {
        'Maut': 9.1,  # 克罗地亚Maut费率
        'Staatliche Abgaben': 11.6,  # 克罗地亚政府附加费
        'Frachtvolumenverhältnis': 200,  # 体积系数
    },
    'Cyprus': {
        'Maut': 7.9,  # 塞浦路斯Maut费率
        'Staatliche Abgaben': 3.6,  # 塞浦路斯政府附加费
        'Frachtvolumenverhältnis': 200,  # 体积系数
    },
    'Denmark': {
        'Maut': 8.6,  # 丹麦Maut费率
        'Staatliche Abgaben': 0.1,  # 丹麦政府附加费
        'Frachtvolumenverhältnis': 200,  # 体积系数
    },
    'Estonia': {
        'Maut': 7.2,  # 爱沙尼亚Maut费率
        'Staatliche Abgaben': 0.0,  # 爱沙尼亚政府附加费
        'Frachtvolumenverhältnis': 200,  # 体积系数
    },
    'Finland': {
        'Maut': 4.8,  # 芬兰Maut费率
        'Staatliche Abgaben': 3.1,  # 芬兰政府附加费
        'Frachtvolumenverhältnis': 200,  # 体积系数
    },
    'France': {
        'Maut': 7.7,  # 法国Maut费率
        'Staatliche Abgaben': 0.5,  # 法国政府附加费
        'Frachtvolumenverhältnis': 200,  # 体积系数
    },
    'Germany': {
        'Maut': 7.8,  # 德国Maut费率
        'Staatliche Abgaben': 10,  # 德国政府附加费
        'Frachtvolumenverhältnis': 200,  # 体积系数
    },
}

# 计算运费的函数
def calculate_shipping_cost(country, weight, zone, is_b2c=False, is_avis=False, is_dangerous_goods=False):
    """
    计算运输费用
    
    :param country: 运输的目的国
    :param weight: 货物重量
    :param zone: 目的地区域
    :param is_b2c: 是否为B2C货物
    :param is_avis: 是否要求电话预约
    :param is_dangerous_goods: 是否为危险品
    :return: 运输费用
    """
    # 获取该国家的规则
    country_rule = hellmann_country_rules.get(country)
    if not country_rule:
        raise ValueError(f"未找到国家 {country} 的规则")

    maut = country_rule['Maut']
    staatliche_abgaben = country_rule['Staatliche Abgaben']
    frachtvolumenverhaeltnis = country_rule['Frachtvolumenverhaeltnis']
    
    # 获取基本运费（以weight和zone为参数，从报价表中获取相应运费）
    base_cost = get_base_cost(country, weight, zone)
    
    # 计算Maut费用和政府附加费
    maut_cost = base_cost * (maut / 100)
    staatliche_abgaben_cost = base_cost * (staatliche_abgaben / 100)
    
    # 计算额外费用
    additional_fee = 0
    if is_b2c:
        additional_fee += 8.9  # B2C附加费用
    if is_avis:
        additional_fee += 12.5  # 电话预约附加费用
    if is_dangerous_goods:
        additional_fee += 15  # 危险品附加费用

    # 计算总费用
    total_cost = base_cost + maut_cost + staatliche_abgaben_cost + additional_fee
    
    return total_cost

def get_base_cost(country, weight, zone):
    """
    获取基本运费。实际使用时，需根据报价表中的价格来计算。
    
    :param country: 目的国
    :param weight: 货物重量
    :param zone: 区域
    :return: 基本运费
    """
    # 基本运费逻辑实现：根据weight和zone从报价表中查找对应价格
    # 这里使用示例逻辑，需要在实际应用中根据报价表获取
    return 100  # 这是一个示例数值，根据实际需求替换为报价表查找逻辑

# 示例：计算一个B2C货物从德国到奥地利，重量500kg，目的区域是Zone 3的运费
cost = calculate_shipping_cost('Germany', 500, 'Zone 3', is_b2c=True, is_avis=False, is_dangerous_goods=True)
print(f"运输费用: {cost} EUR")

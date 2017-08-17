# -*- coding: utf-8 -*-

import json

from odoo import http, exceptions
from odoo.http import request

from .error_code import error_code
from .. import defs


class OrderCreate(http.Controller):
    @http.route('/<string:sub_domain>/order/create',
                auth='public', methods=['POST'], csrf=False, type='http')
    def post(self, sub_domain, **kwargs):
        try:
            user = request.env['res.users'].sudo().search([('sub_domain', '=', sub_domain)])
            if not user:
                return request.make_response(json.dumps({'code': 404, 'msg': error_code[404]}))

            if 'token' not in kwargs.keys():
                return request.make_response(json.dumps({'code': 300, 'msg': error_code[300].format('token')}))

            token = kwargs.pop('token')

            args_key_set = {'remark', 'city_id', 'linkman', 'phone',
                            'goods_json_str', 'address', 'province_id', 'postcode'}

            missing_args_key = args_key_set - set(kwargs.keys())
            if missing_args_key:
                return request.make_response(json.dumps({'code': 600, 'msg': error_code[600]}))

            access_token = request.env(user=user.id)['wechat_mall.access_token'].search([
                ('token', '=', token),
                ('create_uid', '=', user.id)
            ])

            if not access_token:
                return request.make_response(json.dumps({'code': 901, 'msg': error_code[901]}))

            wechat_user = request.env(user=user.id)['wechat_mall.user'].search([
                ('open_id', '=', access_token.open_id),
                ('create_uid', '=', user.id)
            ])

            if not wechat_user:
                return request.make_response(json.dumps({'code': 10000, 'msg': error_code[10000]}))

            # 处理商品json数据
            goods_json = json.loads(kwargs.pop('goods_json_str'))
            province_id = int(kwargs.pop('province_id'))
            city_id = int(kwargs.pop('city_id'))
            district_id = int(kwargs.pop('district_id')) if 'district_id' in kwargs.keys() else False

            goods_price, logistics_price, total, goods_list = self._handle_goods_json(
                goods_json, province_id, city_id, district_id
            )

            order_dict = {
                'wechat_user_id': wechat_user.id,
                'goods_ids': [(4, list(set(map(lambda r: r['goods_id'], goods_list))))],
                'number_goods': sum(map(lambda r: r['amount'], goods_list)),
                'goods_price': goods_price,
                'logistics_price': logistics_price,
                'total': total,
                'province_id': province_id,
                'city_id': city_id,
                'district_id': district_id
            }
            order_dict.update(kwargs)

            order = request.env(user=user.id)['wechat_mall.order'].create(order_dict)

            for each_goods in goods_list:
                each_goods['order_id'] = order.id
                request.env(user=user.id)['wechat_mall.order.goods'].create(each_goods)

            return request.make_response(json.dumps({'code': 0, 'msg': 'success'}))

        except Exception as e:
            return request.make_response(json.dumps({'code': -1, 'msg': error_code[-1], 'data': e.message}))

    def _handle_goods_json(self, goods_json, province_id, city_id, district_id):
        """
        处理订单创建请求的商品json数据，将其转换为可以直接生成'wechat_mall.order.goods'模型数据的字典，并返回商品总价和物流费用
        :param goods_json: dict
        :param province_id: 省
        :param city_id: 市
        :param district_id: 区
        :return:goods_price, logistics_price, total, goods_list
        """
        goods_price, logistics_price = 0.0, 0.0
        goods_id_set = set(map(lambda r: r['goods_id'], goods_json))
        goods_dict = {goods.id: goods
                      for goods in
                      request.env['wechat_mall.goods'].browse(list(goods_id_set))}
        goods_list = []

        if set(goods_dict.keys()) - goods_id_set:
            raise exceptions.ValidationError('订单中存在已下架的商品，请重新下单。')

        for each_goods in goods_json:
            property_child_ids = each_goods['property_child_ids']
            amount = each_goods['amount']
            transport_type = each_goods['transport_type']

            each_goods_price, each_goods_total, property_str = self._count_goods_price(
                goods_dict[each_goods['goods_id']], amount, property_child_ids
            )
            each_logistics_price = self._count_logistics_price(
                goods_dict[each_goods['goods_id']], amount, transport_type, province_id, city_id, district_id
            )
            goods_list.append({
                'goods_id': goods_dict[each_goods['goods_id']].id,
                'name': goods_dict[each_goods['goods_id']].name,
                'pic': goods_dict[each_goods['goods_id']].pic[0].id if
                goods_dict[each_goods['goods_id']].pic else False,
                'property_str': property_str,
                'price': each_goods_price,
                'amount': amount,
                'total': each_goods_total
            })
            goods_price += each_goods_total
            logistics_price += each_logistics_price

        return goods_price, logistics_price, goods_price + logistics_price, goods_list

    def _count_goods_price(self, goods, amount, property_child_ids):
        """
        计算商品价格
        :param goods: model('wechat_mall.goods')
        :param amount: int
        :param property_child_ids: string
        :return: price, total, property_str
        """
        property_child = goods.price_ids.filtered(lambda r: r.property_child_ids == property_child_ids)
        price = property_child.price
        property_str = property_child.name
        total = price * amount
        stores = property_child.stores - amount
        if stores < 0:
            raise exceptions.ValidationError('库存不足请重新下单！')

        if stores == 0:
            # todo 发送库存空邮件
            pass

        property_child.sudo().write({'stores': stores})

        return price, total, property_str

    def _count_logistics_price(self, goods, amount, transport_type, province_id, city_id, district_id):
        """
        计算物流费用
        :param goods: model('wechat_mall.goods')
        :param amount: int
        :param transport_type: string
        :return: price
        """
        if goods.logistics_id.free:
            return 0

        # 保证运输费是最精确的地址匹配
        transport = goods.logistics_id.district_transportation_ids.filtered(
            lambda r: r.default_transportation_id.transport_type == defs.TransportRequestType.attrs[transport_type]
                      and r.province_id.id == province_id
                      and r.city_id.id == city_id
                      and r.district_id.id in [district_id, False]
        ).sorted(lambda r: not r.district_id)

        if not transport:
            transport = goods.logistics_id.transportation_ids.filtered(
                lambda r: r.transport_type == defs.TransportRequestType.attrs[transport_type]
            )

        if not transport:
            return 0

        transport = transport[0]

        # 按重量计数
        if defs.LogisticsValuationRequestType.attrs[transport_type] == defs.LogisticsValuationType.by_weight:
            amount = amount * goods.weight

        if amount <= transport.less_amount:
            return transport.less_price
        else:
            return transport.less_price + \
                   int(((amount - transport.less_amount) / transport.increase_amount)) * transport.increase_price